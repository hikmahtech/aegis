"""Notification budget (Phase 5) — cap the daily volume of proactive FYI pushes.

Every proactive notification (drift / cert / backup / renewal / digest) goes
through `safe_send_message`, which consults this gate. While `enabled` is False
the gate only RECORDS (so you can measure the volume); flip it on to actually
defer over-budget pushes to the daily digest (the design's "3-5/day" cap).
"""

from __future__ import annotations

from typing import Any


async def count_today(pool: Any) -> int:
    """Proactive notifications actually sent so far today (across all agents)."""
    n = await pool.fetchval(
        "SELECT count(*) FROM notification_log "
        "WHERE sent AND created_at >= date_trunc('day', now())"
    )
    return int(n or 0)


async def record_notification(pool: Any, agent_id: str, log_event: str, sent: bool) -> None:
    await pool.execute(
        "INSERT INTO notification_log (agent_id, log_event, sent) VALUES ($1,$2,$3)",
        agent_id,
        log_event,
        sent,
    )


async def should_send(pool: Any, *, enabled: bool, daily_budget: int) -> tuple[bool, int]:
    """Return (allow, count_today). When disabled, always allow (record-only)."""
    n = await count_today(pool)
    if not enabled:
        return True, n
    return (n < daily_budget), n


async def budget_status(pool: Any, *, enabled: bool, daily_budget: int) -> dict:
    """For the admin surface: today's proactive count vs the budget."""
    sent = await count_today(pool)
    deferred = await pool.fetchval(
        "SELECT count(*) FROM notification_log "
        "WHERE NOT sent AND created_at >= date_trunc('day', now())"
    )
    return {
        "enabled": enabled,
        "daily_budget": daily_budget,
        "sent_today": sent,
        "deferred_today": int(deferred or 0),
    }
