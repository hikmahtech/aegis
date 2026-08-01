"""Memory activities — the nightly cap, plus the A3 consolidation *planner*.

`consolidate_agent_memories` PROPOSES an ADD/UPDATE/DELETE/NOOP plan over one
agent's `agent_memory` rows and returns it. It never applies it.

That is structural, not a setting:

* nothing in this module issues DELETE / UPDATE / INSERT against
  `agent_memory` — the only read helper it calls is
  `aegis.services.memory.all_memories`, a SELECT;
* `aegis.services.memory` has no `apply_consolidation` function to call;
* `dry_run=False` is refused unconditionally (see `_NON_DRY_RUN_REFUSAL`).

Deleting the refusal alone would still change nothing, because there is no
apply path behind it. Unlocking real consolidation is A4's job and needs all
three of the changes listed in `_NON_DRY_RUN_REFUSAL`.

Rationale: these rows are the user's own accumulated knowledge, largely
human-authored corrections. A wrong DELETE is unrecoverable, and the plan is
produced by an LLM — so a malformed or truncated response must degrade to an
empty plan, never to a delete list. `_validate_ops` enforces that.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

_VALID_OPS = ("ADD", "UPDATE", "DELETE", "NOOP")
_MAX_CONTENT = 2000
_MAX_OPS = 200

_NON_DRY_RUN_REFUSAL = (
    "memory_consolidation_non_dry_run_refused: A3 is a planner only and has no "
    "apply path. Unlocking dry_run=False is A4 and requires ALL of: "
    "(1) a migration adding agent_memory.superseded_by, "
    "agent_memory.last_consolidated_at and the agent_memory_ops_log table; "
    "(2) aegis.services.memory.apply_consolidation implementing DELETE as a soft "
    "retire plus the delete quotas (max_ops_pct, min_age_hours, importance and "
    "'[gmail:...]' dedupe-marker protection); "
    "(3) removing this refusal in MemoryActivities.consolidate_agent_memories. "
    "Until then no config value, env var or DB state can make this activity write."
)

_SYSTEM_PROMPT = (
    "You consolidate an AI agent's long-term memory. You are given a JSON array "
    "of memory rows: {id, content, importance, source}. Propose the smallest set "
    "of operations that removes duplication and contradiction while LOSING NO "
    "distinct fact.\n"
    "Return ONLY a JSON array of operations, each one of:\n"
    '  {"op":"UPDATE","id":<id>,"content":"<merged text>","importance":<0-1>}\n'
    '  {"op":"DELETE","id":<id>,"reason":"<why redundant>"}\n'
    '  {"op":"ADD","content":"<new generalisation>","importance":<0-1>}\n'
    '  {"op":"NOOP"}\n'
    "Rules: only DELETE a row whose every fact survives in another row or in an "
    "UPDATE you also emit; prefer the NEWER statement when two rows contradict; "
    "never invent ids. If nothing should change, return []."
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
    """
    if not isinstance(raw, list):
        return [], 0
    ops: list[dict] = []
    skipped = 0
    for item in raw[:_MAX_OPS]:
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
            ops.append(
                {"op": "DELETE", "id": memory_id, "reason": str(item.get("reason") or "")[:200]}
            )
    return ops, skipped


@dataclass
class MemoryActivities:
    db_pool: Any
    llm_client: Any = None
    model: str = "gpt-oss:20b"

    @activity.defn
    async def prune_agent_memories(self, keep: int = 50) -> dict:
        """Cap each active agent's memory at `keep` rows (importance, then recency)."""
        from aegis.services.memory import prune_memories

        rows = await self.db_pool.fetch("SELECT id FROM agents WHERE active = TRUE")
        total = 0
        for r in rows:
            total += await prune_memories(self.db_pool, r["id"], keep)
        activity.logger.info("memory_pruned total=%s agents=%s keep=%s", total, len(rows), keep)
        return {"status": "ok", "pruned": total, "agents": len(rows)}

    @activity.defn
    async def consolidate_agent_memories(self, agent_id: str, dry_run: bool = True) -> dict:
        """Propose — never apply — a consolidation plan for one agent's memories.

        Returns `{status, ops, applied, skipped, dry_run}`. `applied` is the
        literal 0: A3 has no apply path (see the module docstring). `dry_run`
        must be True; False is refused non-retryably until A4 lands.
        """
        if not dry_run:
            raise ApplicationError(_NON_DRY_RUN_REFUSAL, non_retryable=True)

        from aegis.services.memory import all_memories

        base = {"applied": 0, "dry_run": True, "ops": [], "skipped": 0}

        memories = await all_memories(self.db_pool, agent_id)
        if not memories:
            return {**base, "status": "skipped", "reason": "no_memories"}
        if not self.llm_client:
            # Documented no-op: the worker injects deps.llm, but a bare
            # MemoryActivities(db_pool=...) stays observe-only rather than crash.
            return {**base, "status": "skipped", "reason": "no_llm_client"}

        from aegis.llm import parse_llm_json
        from aegis.observability import record_llm_call

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
        started = time.monotonic()
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

        await record_llm_call(
            self.db_pool,
            model=result.get("model", self.model),
            prompt_tokens=result.get("prompt_tokens", 0),
            completion_tokens=result.get("completion_tokens", 0),
            latency_ms=int((time.monotonic() - started) * 1000),
            purpose="memory_consolidation",
            agent_id=agent_id,
        )

        parsed = parse_llm_json(result.get("response") or "")
        ops, skipped = _validate_ops(parsed, {m["id"] for m in memories})
        activity.logger.info(
            "memory_consolidation_planned agent=%s memories=%s ops=%s skipped=%s applied=0",
            agent_id,
            len(memories),
            len(ops),
            skipped,
        )
        return {
            **base,
            "status": "ok" if parsed is not None else "unparseable",
            "ops": ops,
            "skipped": skipped,
            "memories": len(memories),
        }
