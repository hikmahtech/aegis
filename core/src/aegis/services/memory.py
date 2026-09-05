"""Per-agent writable memory — the learning loop (Phase 4: memory that learns).

Agents accumulate durable lessons from human corrections (interaction
resolutions that carry a reason) and surface the top ones in their chat system
prompt, so they get better at the owner over time. Writes are plain-text rows —
human-auditable and prunable (the memory-poisoning guard from the design).
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

# Response fields that carry a human correction worth remembering.
_CORRECTION_KEYS = ("reason", "note", "feedback", "comment", "correction")

# `interactions.origin` values whose cards must NEVER become agent memory.
#
# The learning loop stores the card's PROMPT verbatim, and an agent-run
# permission card's prompt embeds up to 800 bytes of the run's pending tool
# input — a file the run just read, a command it composed, text from a web page
# it fetched. Denying that input and then persisting it into the agent's system
# prompt is prompt injection with a delay: the refused content ends up in front
# of the model anyway, on every later turn, labelled as something the human
# taught it.
#
# Skipping (rather than redacting down to the tool name) is the right shape
# because the lesson has no value to begin with: "the human denied Bash once"
# generalises to nothing, the operator's note is about that one call, and the
# run that raised the card is long finished. `origin` is a real column on
# `interactions` and the gate already sets it, so no new plumbing is needed.
_UNLEARNABLE_ORIGINS = frozenset({"agent_run_gate"})


async def record_memory(
    pool: Any, agent_id: str, content: str, importance: float = 0.5, source: str = "correction"
) -> None:
    """Write one durable lesson. Re-writing the same lesson is a NO-OP.

    This runs inside `InteractionFlow`'s post-resolve hook, which Temporal
    retries (`maximum_attempts=2`), and the curiosity hook's 240s books budget
    can expire mid-flight — so attempt 2 arrives with byte-identical content
    (issue #389). `ON CONFLICT DO NOTHING` against the partial unique index
    from migration 028 keeps that a single belief.

    Deliberately NOT a SELECT-then-INSERT: the check-then-write race is exactly
    what a concurrent retry reproduces. The `WHERE` clause is not optional — an
    `ON CONFLICT` arbiter for a PARTIAL index has to repeat that index's
    predicate, or Postgres cannot infer it and raises.
    """
    content = (content or "").strip()
    if not content:
        return
    await pool.execute(
        "INSERT INTO agent_memory (agent_id, content, importance, source) VALUES ($1,$2,$3,$4) "
        "ON CONFLICT (agent_id, md5(content)) WHERE superseded_at IS NULL DO NOTHING",
        agent_id,
        content[:2000],
        float(importance),
        source,
    )


async def recent_memories(pool: Any, agent_id: str, limit: int = 8) -> list[str]:
    # `superseded_at IS NULL` = live rows only. A consolidation pass retires a
    # row instead of deleting it (migration 020), so a retired row must vanish
    # from the system prompt while staying restorable in the table.
    rows = await pool.fetch(
        "SELECT content FROM agent_memory WHERE agent_id = $1 AND superseded_at IS NULL "
        "ORDER BY importance DESC, created_at DESC LIMIT $2",
        agent_id,
        limit,
    )
    return [r["content"] for r in rows]


async def all_memories(pool: Any, agent_id: str) -> list[dict[str, Any]]:
    """Every memory row for one agent, with the metadata consolidation needs.

    `recent_memories` deliberately returns bare content strings (it feeds a
    system prompt). A consolidation pass has to reference rows by id and weigh
    them, so it needs id / importance / source / created_at too.

    Read-only, and LIVE rows only (`superseded_at IS NULL`) — an already
    soft-retired row must not be re-proposed for deletion, and must not count
    toward the consolidation quota denominator.
    """
    rows = await pool.fetch(
        "SELECT id, content, importance, source, created_at FROM agent_memory "
        "WHERE agent_id = $1 AND superseded_at IS NULL "
        "ORDER BY importance DESC, created_at DESC",
        agent_id,
    )
    return [
        {
            "id": int(r["id"]),
            "content": r["content"],
            "importance": float(r["importance"]),
            "source": r["source"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def format_memories(memories: list[str]) -> str:
    if not memories:
        return ""
    lines = "\n".join(f"- {m}" for m in memories)
    return f"\n\n## What you've learned (from past corrections)\n{lines}\n"


async def record_correction_from_interaction(
    pool: Any, agent_id: str, prompt: str, response: Any, origin: str = ""
) -> None:
    """Save a durable lesson when a resolved interaction carries a human
    correction (a reason/note/feedback). No-op for bare accepts, and for any
    card whose `origin` is in `_UNLEARNABLE_ORIGINS` — its prompt quotes
    untrusted content. Never raises — a memory write must not break interaction
    resolution."""
    if str(origin or "") in _UNLEARNABLE_ORIGINS:
        logger.debug("agent_memory_skipped_untrusted_origin", agent_id=agent_id, origin=origin)
        return
    if not isinstance(response, dict):
        return
    reason = next(
        (str(response[k]).strip() for k in _CORRECTION_KEYS if response.get(k)), ""
    )
    if not reason:
        return
    value = str(response.get("value") or response.get("action") or "responded")
    content = f'When asked "{(prompt or "")[:200]}", the human chose \'{value}\': {reason}'
    try:
        await record_memory(
            pool, agent_id, content, importance=0.7, source="interaction_correction"
        )
        logger.info("agent_memory_recorded", agent_id=agent_id, source="interaction_correction")
    except Exception as exc:  # noqa: BLE001 — memory write must never break resolve
        logger.warning("agent_memory_record_failed", error=str(exc)[:200])


async def record_gmail_triage_correction(
    pool: Any, agent_id: str, email_id: str, subject: str, predicted: str, actual: str
) -> bool:
    """Write an agent_memory row for a REAL Gmail triage correction (#116) —
    the user re-labeled an email AEGIS mis-triaged, detected by the recheck
    loop (`recheck_triage_outcomes`) without any manual note. This is the
    correction signal that fires without user effort, unlike
    `record_correction_from_interaction` which needs a free-text reason
    nobody types.

    Idempotent per email_id: a dedupe marker is embedded in `content` and
    checked before insert, so a re-run (e.g. the same message rechecked
    twice) does not write a second row. Never raises.
    """
    dedupe_marker = f"[gmail:{email_id}]"
    try:
        # Deliberately NOT filtered on `superseded_at IS NULL`: a retired row
        # still proves this email was already recorded, so retirement can never
        # resurrect a duplicate. (Consolidation also refuses to touch rows
        # carrying this marker at all — see _DEDUPE_MARKER in the A4 planner.)
        exists = await pool.fetchval(
            "SELECT 1 FROM agent_memory WHERE agent_id = $1 AND content LIKE $2 LIMIT 1",
            agent_id,
            f"%{dedupe_marker}",
        )
        if exists:
            return False
        subject_trunc = (subject or "(no subject)").strip()[:80]
        content = (
            f"User corrected email triage: '{subject_trunc}' — predicted {predicted}, "
            f"actually {actual}. {dedupe_marker}"
        )
        await record_memory(
            pool, agent_id, content, importance=0.7, source="gmail_triage_correction"
        )
        logger.info("agent_memory_recorded", agent_id=agent_id, source="gmail_triage_correction")
        return True
    except Exception as exc:  # noqa: BLE001 — memory write must never break recheck
        logger.warning("agent_memory_record_failed", error=str(exc)[:200])
        return False


async def prune_memories(pool: Any, agent_id: str, keep: int = 50) -> int:
    """Cap an agent's LIVE memory at `keep` rows (highest importance, then
    recency). Returns the number deleted. The nightly MemoryReflectionFlow
    calls this.

    Soft-retired rows are excluded from BOTH sides of the cap:

    * they do not consume the `keep` budget (they are invisible to every
      reader, so holding a slot would silently shrink working memory);
    * they are never deleted here. This is load-bearing —
      `MemoryReflectionFlow` runs consolidation and then this prune in the SAME
      pass, so a retire-blind prune would hard-delete everything consolidation
      just retired and "soft retire" would be a lie within one nightly run.

    Retired rows are hard-deleted only by `purge_retired_memories`, which is
    separately gated and disabled by default.
    """
    status = await pool.execute(
        "DELETE FROM agent_memory WHERE agent_id = $1 AND superseded_at IS NULL "
        "AND id NOT IN ("
        "  SELECT id FROM agent_memory WHERE agent_id = $1 AND superseded_at IS NULL "
        "  ORDER BY importance DESC, created_at DESC LIMIT $2)",
        agent_id,
        keep,
    )
    return int(status.split()[-1]) if status else 0


async def purge_retired_memories(pool: Any, agent_id: str, older_than_days: int) -> int:
    """Hard-delete soft-retired rows retired more than `older_than_days` ago.

    THE ONLY IRREVERSIBLE DELETE ON THIS PATH, and it is gated three ways:

    1. `older_than_days <= 0` is a no-op — and that is the shipped default
       (`MemoryReflectionInput.retire_grace_days = 0`), so this function does
       nothing at all unless an operator deliberately sets a grace window;
    2. it only ever touches rows that a consolidation pass already retired
       (`superseded_at IS NOT NULL`), never a live row;
    3. the row must have been retired for the full grace window, giving a
       real recovery period rather than a theoretical one.

    Even after a purge, `agent_memory_ops_log.before_content` still holds the
    text of the retired row — that ledger is deliberately never pruned.

    Returns the number of rows deleted.
    """
    try:
        days = int(older_than_days)
    except (TypeError, ValueError):
        return 0
    if days <= 0:
        return 0
    status = await pool.execute(
        "DELETE FROM agent_memory WHERE agent_id = $1 AND superseded_at IS NOT NULL "
        "AND superseded_at < now() - make_interval(days => $2)",
        agent_id,
        days,
    )
    return int(status.split()[-1]) if status else 0


# --------------------------------------------------------------------------
# A4 — applying a consolidation plan.
# --------------------------------------------------------------------------

# Statements this module is willing to issue against agent_memory on behalf of
# a plan. Every one of them is scoped by `agent_id = $1` IN THE SQL ITSELF, so
# a plan naming another agent's row cannot mutate it even if the caller's
# validation is wrong or absent. That is deliberate belt-and-braces: the
# planner already rejects foreign ids (`_validate_ops`), and this layer refuses
# them again independently.
_SQL_RETIRE = (
    "UPDATE agent_memory SET superseded_at = now(), superseded_by = $3, "
    "last_consolidated_at = now() "
    "WHERE id = $2 AND agent_id = $1 AND superseded_at IS NULL"
)
_SQL_UPDATE = (
    "UPDATE agent_memory SET content = $3, importance = $4, last_consolidated_at = now() "
    "WHERE id = $2 AND agent_id = $1 AND superseded_at IS NULL"
)
# DO NOTHING, not a raise, on the migration-028 index. The whole plan runs in
# ONE transaction ("applied whole or not at all"), so an ADD that restates a
# live belief must not take every other op down with it. `INSERT 0 0` is a
# status `_run` already reads as `no_rows_affected`, so the ledger records the
# skip and the operator sees what happened.
_SQL_ADD = (
    "INSERT INTO agent_memory (agent_id, content, importance, source, last_consolidated_at) "
    "VALUES ($1, $2, $3, 'consolidation', now()) "
    "ON CONFLICT (agent_id, md5(content)) WHERE superseded_at IS NULL DO NOTHING"
)
_SQL_LOG = (
    "INSERT INTO agent_memory_ops_log "
    "(agent_id, run_id, op, memory_id, before_content, after_content, "
    " before_importance, after_importance, dry_run, applied, skip_reason) "
    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)"
)


async def apply_consolidation(
    pool: Any,
    agent_id: str,
    decisions: list[dict[str, Any]],
    *,
    run_id: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Write a vetted consolidation plan, and log every op either way.

    `decisions` come from the planner's safety pass
    (`aegis_worker.activities.memory.decide_ops`). Each is a validated op plus
    `apply: bool` and, when False, a `skip_reason`. This function makes NO
    policy decisions of its own about *which* ops are reasonable — it enforces
    only the two invariants that must hold no matter what the policy said:

    * every mutation is scoped to `agent_id` in SQL (see the constants above);
    * a DELETE op is a SOFT RETIRE, never a row removal.

    `dry_run=True` writes NOTHING to `agent_memory` — only the ledger rows that
    record what would have happened. That is the intended long-running mode.

    All writes happen in ONE transaction: a plan is applied whole or not at
    all. A partially-applied consolidation (some rows merged, their duplicates
    still live, or vice versa) is worse than an unapplied one.

    Returns `{applied, logged, dry_run}`.
    """
    if not decisions:
        return {"applied": 0, "logged": 0, "dry_run": dry_run}

    # Pre-images, re-read under this agent's id. An id that is not a live row
    # of THIS agent yields no pre-image and is downgraded to a skip — the
    # second, SQL-side ownership check.
    wanted = [int(d["id"]) for d in decisions if d.get("id") is not None]
    pre: dict[int, Any] = {}
    if wanted:
        rows = await pool.fetch(
            "SELECT id, content, importance FROM agent_memory "
            "WHERE agent_id = $1 AND id = ANY($2::bigint[]) AND superseded_at IS NULL",
            agent_id,
            wanted,
        )
        pre = {int(r["id"]): r for r in rows}

    applied = 0
    logged = 0

    async def _run(exec_one: Any) -> None:
        nonlocal applied, logged
        for d in decisions:
            op = d["op"]
            memory_id = d.get("id")
            before = pre.get(int(memory_id)) if memory_id is not None else None
            skip_reason = d.get("skip_reason")
            do_apply = bool(d.get("apply")) and not dry_run

            if do_apply and memory_id is not None and before is None:
                # Names a row that is not a live row of this agent.
                do_apply, skip_reason = False, "row_not_found"

            did = False
            if do_apply:
                if op == "DELETE":
                    status = await exec_one(
                        _SQL_RETIRE, agent_id, int(memory_id), d.get("merged_into")
                    )
                elif op == "UPDATE":
                    status = await exec_one(
                        _SQL_UPDATE,
                        agent_id,
                        int(memory_id),
                        d["content"],
                        float(d.get("importance", 0.5)),
                    )
                elif op == "ADD":
                    status = await exec_one(
                        _SQL_ADD, agent_id, d["content"], float(d.get("importance", 0.5))
                    )
                else:  # NOOP — nothing to write, but still worth a ledger row.
                    status = ""
                did = op != "NOOP" and bool(status) and not status.endswith(" 0")
                if did:
                    applied += 1
                elif op != "NOOP":
                    skip_reason = skip_reason or "no_rows_affected"

            await exec_one(
                _SQL_LOG,
                agent_id,
                run_id,
                op,
                memory_id,
                before["content"] if before is not None else None,
                d.get("content"),
                float(before["importance"]) if before is not None else None,
                float(d["importance"]) if d.get("importance") is not None else None,
                dry_run,
                did,
                skip_reason,
            )
            logged += 1

    if dry_run:
        # Ledger-only: no agent_memory statement is reachable from here, so
        # there is nothing to make atomic.
        await _run(pool.execute)
    else:
        async with pool.acquire() as conn, conn.transaction():
            await _run(conn.execute)

    logger.info(
        "agent_memory_consolidation_logged",
        agent_id=agent_id,
        applied=applied,
        logged=logged,
        dry_run=dry_run,
    )
    return {"applied": applied, "logged": logged, "dry_run": dry_run}


async def list_memory_ops(
    pool: Any, agent_id: str, limit: int = 100, applied_only: bool = False
) -> list[dict[str, Any]]:
    """The consolidation ledger for one agent, newest first.

    This is the evidence an operator reads for a week of dry-run output before
    deciding whether to enable apply mode.
    """
    limit = max(1, min(int(limit), 500))
    where = "WHERE agent_id = $1" + (" AND applied = TRUE" if applied_only else "")
    rows = await pool.fetch(
        "SELECT id, run_id, op, memory_id, before_content, after_content, "
        "before_importance, after_importance, dry_run, applied, skip_reason, created_at "
        f"FROM agent_memory_ops_log {where} ORDER BY created_at DESC, id DESC LIMIT $2",
        agent_id,
        limit,
    )
    return [dict(r) for r in rows]
