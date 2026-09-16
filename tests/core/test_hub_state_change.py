"""`hub.record_state_change` — the one writer of the hub's own timeline.

Seven places used to write this INSERT by hand (`ingest_event`, the
suppression promoter, `set_status`, `mute_problem`, `merge_problems`, both
closers) and `hub_group` an eighth. What every one of them depends on is
here: a `state_change` row attributed to `hub`, and idempotency on
`(source, external_id)` so a retried write is a no-op, never a second entry
in the timeline a human reads.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from aegis.services.hub import record_state_change

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


async def _problem(pool) -> str:
    return await pool.fetchval(
        "INSERT INTO problems (correlation_key, class, subject, subject_kind, title, "
        "severity, status, first_seen_at, last_seen_at, occurrences) "
        "VALUES ($1, 'manual', $1, 'service', 'state change test', 'warning', 'open', "
        "now(), now(), 1) RETURNING id::text",
        f"state-change-{uuid.uuid4().hex[:8]}",
    )


async def test_it_writes_one_hub_state_change_row(db_pool):
    problem_id = await _problem(db_pool)
    await record_state_change(
        db_pool,
        problem_id,
        f"mute:{problem_id}:{NOW.isoformat()}",
        severity="warning",
        payload={"action": "mute", "by": "someone"},
        occurred_at=NOW,
    )
    row = await db_pool.fetchrow(
        "SELECT source, kind, severity, payload, occurred_at FROM problem_events "
        "WHERE problem_id = $1::uuid",
        problem_id,
    )
    assert row["source"] == "hub"
    assert row["kind"] == "state_change"
    assert row["severity"] == "warning"
    assert row["payload"] == {"action": "mute", "by": "someone"}
    assert row["occurred_at"] == NOW


async def test_the_same_write_twice_leaves_one_row(db_pool):
    """A retried transition must not double the timeline."""
    problem_id = await _problem(db_pool)
    external_id = f"close:{problem_id}:{NOW.isoformat()}"
    for _ in range(2):
        await record_state_change(
            db_pool,
            problem_id,
            external_id,
            severity="info",
            payload={"action": "close", "reason": "resolved long ago"},
            occurred_at=NOW,
        )
    assert (
        await db_pool.fetchval(
            "SELECT count(*) FROM problem_events WHERE problem_id = $1::uuid", problem_id
        )
        == 1
    )
