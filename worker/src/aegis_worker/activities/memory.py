"""Memory activities — the nightly cap, plus the A3 planner and A4's rails.

`consolidate_agent_memories` proposes an ADD/UPDATE/DELETE/NOOP plan over one
agent's `agent_memory` rows. A3 shipped it observe-only; A4 (this module) adds
the ability to APPLY that plan — behind rails designed so that a wrong plan is
RECOVERABLE, not merely unlikely.

The rails, in the order they fire:

1. **Default off, twice.** Writing needs BOTH `dry_run=false` on the
   `memory-reflection-nightly` row in `activities.config` (DB-owned — edit it
   on /admin/flows; editing `config/seed/activities.yaml` does NOT reach a
   running deployment) AND `AEGIS_MEMORY_CONSOLIDATION_APPLY_ENABLED=true` in
   the worker environment. Two keys in two different systems: a misclick in
   the admin UI cannot grant write access to the user's accumulated memory,
   and an operator can kill writes fleet-wide with an env change + restart
   without touching the DB. With the env switch off the pass silently degrades
   to dry-run (`status="apply_disabled"`) rather than failing the nightly run.
2. **Validation** (`_validate_ops`, from A3): anything that is not an
   unambiguously well-formed op over one of THIS agent's own live row ids is
   dropped. A truncated, prose-wrapped, oversized or otherwise malformed
   response yields an EMPTY plan — it can never be read as a delete list.
3. **Quotas** (`decide_ops`): a plan whose destructive-op count exceeds
   `max_ops_pct` of the agent's live rows (or `_MAX_APPLY_OPS` in absolute
   terms) is refused WHOLESALE. It is never truncated to the first N — a
   truncated plan is a partially-applied consolidation, which is worse than
   none.
4. **Protected rows** (`decide_ops`): rows younger than `min_age_hours`, rows
   at or above `_PROTECT_IMPORTANCE`, and rows carrying the `[gmail:<id>]`
   dedupe marker that `record_gmail_triage_correction` relies on are dropped
   from the op set individually.
5. **Soft retire** (`aegis.services.memory.apply_consolidation`): DELETE sets
   `superseded_at`, it does not remove the row. Reads filter retired rows out;
   the row itself stays restorable. Every mutating statement is additionally
   scoped by `agent_id` in SQL, independently of step 2.
6. **Audit** — every PROPOSED op lands in `agent_memory_ops_log` with its
   before/after content, in dry-run as well as apply mode, including the ops a
   rail refused and why.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from temporalio import activity

_VALID_OPS = ("ADD", "UPDATE", "DELETE", "NOOP")
_MAX_CONTENT = 2000
_MAX_OPS = 200

# --- A4 safety constants -------------------------------------------------
# Deliberately NOT operator-configurable. `max_ops_pct` and `min_age_hours`
# are threaded through activities.config so the rails can be TIGHTENED from
# the admin UI; these three are the floor that a UI edit cannot widen.

# Hard ceiling on non-NOOP ops in a single applied plan, whatever the
# percentage works out to. A plan bigger than this is not a consolidation.
_MAX_APPLY_OPS = 25
# Rows at or above this importance are never merged or retired automatically.
_PROTECT_IMPORTANCE = 0.9
# Below this many live rows, nothing is consolidated at all: on a small memory
# every op is a large fraction of everything the agent knows, and the
# percentage quota stops being a meaningful bound.
_MIN_MEMORIES_TO_APPLY = 10
# `record_gmail_triage_correction` embeds `[gmail:<id>]` in the content and
# dedupes on `content LIKE '%[gmail:<id>]'`. Rewriting or retiring such a row
# would break that idempotence, so consolidation never touches one.
_DEDUPE_MARKER = "[gmail:"

_SYSTEM_PROMPT = (
    "You consolidate an AI agent's long-term memory. You are given a JSON array "
    "of memory rows: {id, content, importance, source}. Propose the smallest set "
    "of operations that removes duplication and contradiction while LOSING NO "
    "distinct fact.\n"
    "Return ONLY a JSON array of operations, each one of:\n"
    '  {"op":"UPDATE","id":<id>,"content":"<merged text>","importance":<0-1>}\n'
    '  {"op":"DELETE","id":<id>,"reason":"<why redundant>","merged_into":<id>}\n'
    '  {"op":"ADD","content":"<new generalisation>","importance":<0-1>}\n'
    '  {"op":"NOOP"}\n'
    "Rules: only DELETE a row whose every fact survives in another row or in an "
    "UPDATE you also emit; on DELETE set merged_into to the id of the row that "
    "absorbed it when there is one; prefer the NEWER statement when two rows "
    "contradict; never invent ids. If nothing should change, return []."
)


def _as_id(value: Any) -> int | None:
    """Strictly coerce an LLM-supplied id. Booleans are not ids."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _clamp(value: Any, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _validate_ops(raw: Any, own_ids: set[int]) -> tuple[list[dict], int]:
    """Defensively turn an LLM payload into a validated op list.

    Anything that is not an unambiguously well-formed op over one of THIS
    agent's own memory ids is dropped and counted in `skipped`. A truncated,
    prose-wrapped or otherwise malformed response parses to `None`/non-list and
    yields an EMPTY plan — it can never be read as a DELETE plan.

    An over-long payload (> `_MAX_OPS`) is rejected ENTIRELY rather than
    truncated: silently keeping the first 200 ops of a 500-op response and
    applying them is a partially-applied consolidation.
    """
    if not isinstance(raw, list):
        return [], 0
    if len(raw) > _MAX_OPS:
        return [], len(raw)
    ops: list[dict] = []
    skipped = 0
    for item in raw:
        if not isinstance(item, dict):
            skipped += 1
            continue
        op = str(item.get("op") or "").strip().upper()
        if op not in _VALID_OPS:
            skipped += 1
            continue
        if op == "NOOP":
            ops.append({"op": "NOOP"})
            continue
        content = str(item.get("content") or "").strip()[:_MAX_CONTENT]
        if op == "ADD":
            if not content:
                skipped += 1
                continue
            ops.append({"op": "ADD", "content": content, "importance": _clamp(item.get("importance"))})
            continue
        # UPDATE / DELETE must name a row this agent actually owns.
        memory_id = _as_id(item.get("id"))
        if memory_id is None or memory_id not in own_ids:
            skipped += 1
            continue
        if op == "UPDATE":
            if not content:
                skipped += 1
                continue
            ops.append(
                {
                    "op": "UPDATE",
                    "id": memory_id,
                    "content": content,
                    "importance": _clamp(item.get("importance")),
                }
            )
        else:
            # `merged_into` is optional provenance (which row absorbed this
            # one). It must also be one of this agent's own ids, and it may not
            # be the row being retired.
            merged_into = _as_id(item.get("merged_into"))
            if merged_into not in own_ids or merged_into == memory_id:
                merged_into = None
            ops.append(
                {
                    "op": "DELETE",
                    "id": memory_id,
                    "reason": str(item.get("reason") or "")[:200],
                    "merged_into": merged_into,
                }
            )
    return ops, skipped


def _protection_reason(
    memory: dict[str, Any], cutoff: datetime, min_age_hours: int
) -> str | None:
    """Why this row may never be rewritten or retired by an LLM, or None."""
    if _DEDUPE_MARKER in (memory.get("content") or ""):
        return "protected_dedupe_marker"
    if float(memory.get("importance") or 0.0) >= _PROTECT_IMPORTANCE:
        return "protected_importance"
    created = memory.get("created_at")
    if min_age_hours > 0 and isinstance(created, datetime) and created > cutoff:
        return "protected_recent"
    return None


def decide_ops(
    memories: list[dict[str, Any]],
    ops: list[dict],
    *,
    max_ops_pct: float,
    min_age_hours: int,
    now: datetime | None = None,
) -> tuple[list[dict], str | None]:
    """Apply A4's quotas and per-row protections to a validated plan.

    Pure — no DB, no clock beyond `now` — so every rail below is testable on
    its own. Returns `(decisions, refusal)`:

    * `decisions` is one entry per proposed op, each carrying `apply: bool` and
      a `skip_reason` when False. Nothing is ever dropped from the list: an op
      that a rail refused still has to reach `agent_memory_ops_log`.
    * `refusal` is non-None when the WHOLE plan is refused, in which case every
      decision has `apply=False`. A quota breach refuses the batch outright
      rather than applying the part that fits.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=max(0, int(min_age_hours)))
    by_id = {int(m["id"]): m for m in memories}
    total = len(memories)

    destructive = [o for o in ops if o["op"] in ("UPDATE", "DELETE")]
    non_noop = [o for o in ops if o["op"] != "NOOP"]

    refusal: str | None = None
    if non_noop and total < _MIN_MEMORIES_TO_APPLY:
        refusal = "too_few_memories"
    elif len(non_noop) > _MAX_APPLY_OPS:
        refusal = "quota_exceeded_abs"
    elif len(destructive) > int(total * max_ops_pct):
        # NOTE: counted over the ops the LLM PROPOSED, before per-row
        # protections are applied. A plan that wants to rewrite half the
        # agent's memory is a bad plan even if most of its targets happen to be
        # protected — the aggressiveness is the signal.
        refusal = "quota_exceeded_pct"

    decisions: list[dict] = []
    for op in ops:
        decision = dict(op)
        if refusal is not None:
            decision["apply"] = False
            decision["skip_reason"] = refusal
        elif op["op"] == "NOOP":
            decision["apply"] = False
            decision["skip_reason"] = "noop"
        elif op["op"] == "ADD":
            decision["apply"] = True
            decision["skip_reason"] = None
        else:
            reason = _protection_reason(by_id[int(op["id"])], cutoff, min_age_hours)
            decision["apply"] = reason is None
            decision["skip_reason"] = reason
        decisions.append(decision)
    return decisions, refusal


@dataclass
class MemoryActivities:
    db_pool: Any
    llm_client: Any = None
    model: str = "gpt-oss:20b"
    # The deployment-level kill switch (AEGIS_MEMORY_CONSOLIDATION_APPLY_ENABLED).
    # False here means no plan can ever be applied, whatever activities.config
    # says. Default False so that merely deploying A4 changes no behaviour.
    apply_enabled: bool = False

    @activity.defn
    async def prune_agent_memories(self, keep: int = 50, retire_grace_days: int = 0) -> dict:
        """Cap each active agent's live memory at `keep` rows, and optionally
        hard-delete rows soft-retired longer than `retire_grace_days` ago.

        `retire_grace_days=0` (the default, and the shipped flow default)
        disables the purge entirely: retired rows accumulate rather than being
        destroyed, which is the reversible choice. An operator who has watched
        the ledger and trusts the pass can set e.g. 30.
        """
        from aegis.services.memory import prune_memories, purge_retired_memories

        rows = await self.db_pool.fetch("SELECT id FROM agents WHERE active = TRUE")
        total = 0
        purged = 0
        for r in rows:
            total += await prune_memories(self.db_pool, r["id"], keep)
            if retire_grace_days and int(retire_grace_days) > 0:
                purged += await purge_retired_memories(
                    self.db_pool, r["id"], int(retire_grace_days)
                )
        activity.logger.info(
            "memory_pruned total=%s purged_retired=%s agents=%s keep=%s",
            total,
            purged,
            len(rows),
            keep,
        )
        return {"status": "ok", "pruned": total, "purged_retired": purged, "agents": len(rows)}

    @activity.defn
    async def consolidate_agent_memories(
        self,
        agent_id: str,
        dry_run: bool = True,
        max_ops_pct: float = 0.25,
        min_age_hours: int = 24,
    ) -> dict:
        """Plan — and, only if both gates are open, apply — a consolidation.

        Returns `{status, ops, applied, skipped, dry_run, ...}`. `dry_run` in
        the result is the EFFECTIVE mode, not the requested one: with the
        environment kill switch off, a requested `dry_run=False` comes back as
        `dry_run=True` with `status="apply_disabled"`.
        """
        from aegis.services.memory import all_memories

        # Gate 1 (config) AND gate 2 (environment). Either one closed ⇒ dry run.
        apply_requested = not dry_run
        effective_dry_run = dry_run or not self.apply_enabled
        if apply_requested and not self.apply_enabled:
            activity.logger.warning(
                "memory_consolidation_apply_disabled agent=%s "
                "reason=AEGIS_MEMORY_CONSOLIDATION_APPLY_ENABLED_not_set",
                agent_id,
            )

        base: dict[str, Any] = {
            "applied": 0,
            "dry_run": effective_dry_run,
            "ops": [],
            "skipped": 0,
        }

        memories = await all_memories(self.db_pool, agent_id)
        if not memories:
            return {**base, "status": "skipped", "reason": "no_memories"}
        if not self.llm_client:
            # Documented no-op: the worker injects deps.llm, but a bare
            # MemoryActivities(db_pool=...) stays observe-only rather than crash.
            return {**base, "status": "skipped", "reason": "no_llm_client"}

        from aegis.llm import parse_llm_json

        payload = json.dumps(
            [
                {
                    "id": m["id"],
                    "content": m["content"][:400],
                    "importance": round(m["importance"], 2),
                    "source": m["source"],
                }
                for m in memories
            ]
        )
        # db_pool + purpose ⇒ think() writes the llm_calls row itself, for
        # success and failure alike (LLMClient._record_call). Do not record here.
        try:
            result = await self.llm_client.think(
                prompt=payload[:12000],
                model=self.model,
                system_prompt=_SYSTEM_PROMPT,
                max_tokens=2500,
                db_pool=self.db_pool,
                purpose="memory_consolidation",
                agent_id=agent_id,
            )
        except Exception as exc:  # noqa: BLE001 — a dead LLM skips the pass, never fails the night
            activity.logger.warning(
                "memory_consolidation_llm_failed agent=%s err=%s", agent_id, str(exc)[:200]
            )
            return {**base, "status": "llm_failed", "error": str(exc)[:200]}

        parsed = parse_llm_json(result.get("response") or "")
        ops, skipped = _validate_ops(parsed, {m["id"] for m in memories})
        decisions, refusal = decide_ops(
            memories,
            ops,
            max_ops_pct=_clamp(max_ops_pct, 0.25),
            min_age_hours=max(0, int(min_age_hours)),
        )

        applied = 0
        logged = 0
        if decisions:
            from aegis.services.memory import apply_consolidation

            outcome = await apply_consolidation(
                self.db_pool,
                agent_id,
                decisions,
                run_id=_run_id(),
                dry_run=effective_dry_run,
            )
            applied = int(outcome["applied"])
            logged = int(outcome["logged"])

        if refusal is not None:
            status = refusal
        elif parsed is None:
            status = "unparseable"
        elif apply_requested and not self.apply_enabled:
            status = "apply_disabled"
        elif applied:
            status = "applied"
        else:
            status = "ok"

        activity.logger.info(
            "memory_consolidation_planned agent=%s memories=%s ops=%s skipped=%s "
            "applied=%s logged=%s status=%s dry_run=%s",
            agent_id,
            len(memories),
            len(ops),
            skipped,
            applied,
            logged,
            status,
            effective_dry_run,
        )
        return {
            **base,
            "status": status,
            "ops": ops,
            "skipped": skipped,
            "applied": applied,
            "logged": logged,
            "refusal": refusal,
            "memories": len(memories),
        }


def _run_id() -> str | None:
    """The Temporal run that proposed the plan, for the ledger. Best effort —
    the activity must not fail because it is running outside a workflow."""
    try:
        return activity.info().workflow_run_id
    except Exception:  # noqa: BLE001
        return None
