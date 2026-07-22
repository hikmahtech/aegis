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


async def record_memory(
    pool: Any, agent_id: str, content: str, importance: float = 0.5, source: str = "correction"
) -> None:
    content = (content or "").strip()
    if not content:
        return
    await pool.execute(
        "INSERT INTO agent_memory (agent_id, content, importance, source) VALUES ($1,$2,$3,$4)",
        agent_id,
        content[:2000],
        float(importance),
        source,
    )


async def recent_memories(pool: Any, agent_id: str, limit: int = 8) -> list[str]:
    rows = await pool.fetch(
        "SELECT content FROM agent_memory WHERE agent_id = $1 "
        "ORDER BY importance DESC, created_at DESC LIMIT $2",
        agent_id,
        limit,
    )
    return [r["content"] for r in rows]


def format_memories(memories: list[str]) -> str:
    if not memories:
        return ""
    lines = "\n".join(f"- {m}" for m in memories)
    return f"\n\n## What you've learned (from past corrections)\n{lines}\n"


async def record_correction_from_interaction(
    pool: Any, agent_id: str, prompt: str, response: Any
) -> None:
    """Save a durable lesson when a resolved interaction carries a human
    correction (a reason/note/feedback). No-op for bare accepts. Never raises —
    a memory write must not break interaction resolution."""
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
    """Cap an agent's memory at `keep` rows (highest importance, then recency).
    Returns the number deleted. The nightly MemoryReflectionFlow calls this."""
    status = await pool.execute(
        "DELETE FROM agent_memory WHERE agent_id = $1 AND id NOT IN ("
        "  SELECT id FROM agent_memory WHERE agent_id = $1 "
        "  ORDER BY importance DESC, created_at DESC LIMIT $2)",
        agent_id,
        keep,
    )
    return int(status.split()[-1]) if status else 0
