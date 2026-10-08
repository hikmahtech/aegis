"""Test helpers for the problem hub.

`mute_problem` is the hub's old mute, kept here as test setup. The mute endpoint
and its callers left v1 with the infra lane (DevOps vertical, a2-devops), but a
`muted_until` set before then is still honoured on ingest and projection until
it runs out, and these tests pin that.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import asyncpg
from aegis.services.hub import _utcnow, record_state_change


async def mute_problem(
    pool: asyncpg.Pool,
    problem_id: str,
    *,
    hours: float,
    by: str,
    reason: str = "",
    now: datetime | None = None,
) -> datetime | None:
    """Silence a problem until ``now + hours``: occurrences are still recorded
    and counted, nothing is projected or investigated. The mute key *is* the
    problem (the old `alert_mutes` table and its four key namespaces are gone). Returns the new
    `muted_until`, or None when the problem is missing or closed."""
    now = now or _utcnow()
    until = now + timedelta(hours=max(float(hours), 0.0))
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "UPDATE problems SET muted_until = $2 WHERE id = $1::uuid AND closed_at IS NULL "
            "RETURNING severity",
            problem_id,
            until,
        )
        if row is None:
            return None
        await record_state_change(
            conn,
            problem_id,
            f"mute:{problem_id}:{now.isoformat()}",
            severity=row["severity"],
            payload={
                "action": "mute",
                "until": until.isoformat(),
                "by": by,
                "reason": reason[:300],
            },
            occurred_at=now,
        )
    return until
