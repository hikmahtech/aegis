"""HubActivities are thin wrappers; these pin the shapes and the no-pool path."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from aegis.services.hub import Event, get_problem, ingest_event, set_service_state
from aegis_worker.activities.hub import HubActivities
from temporalio.testing import ActivityEnvironment

pytestmark = pytest.mark.asyncio

# The activities run on the real clock, so the seeded window must be over in
# real time, not merely relative to a fixed test "now".
LONG_AGO = datetime(2026, 9, 7, 12, 0, tzinfo=UTC) - timedelta(days=30)


async def test_no_pool_is_a_quiet_noop():
    env = ActivityEnvironment()
    act = HubActivities(db_pool=None)
    assert await env.run(act.promote_expired_suppressions) == {"promoted": 0, "problem_ids": []}
    assert await env.run(act.clear_converged_deploys, ["x"]) == {"cleared": []}


async def test_promote_and_clear_round_trip(db_pool):
    s = f"svc_{uuid.uuid4().hex[:8]}"
    # A window that expired an hour ago, with a problem seen inside it.
    await set_service_state(db_pool, s, "deploying", minutes=5, set_by="ansible", now=LONG_AGO)
    r = await ingest_event(
        db_pool,
        Event(source="heartbeat", external_id=f"{s}@1", kind="occurrence", title="down", klass="DockerServiceDown", subject=s, occurred_at=LONG_AGO),
        now=LONG_AGO,
    )
    assert (await get_problem(db_pool, r.problem_id))["status"] == "suppressed"

    env = ActivityEnvironment()
    act = HubActivities(db_pool=db_pool)
    out = await env.run(act.promote_expired_suppressions)
    assert r.problem_id in out["problem_ids"] and out["promoted"] >= 1
    assert (await get_problem(db_pool, r.problem_id))["status"] == "open"

    # An old open-ended deploying row for a converged service is cleared.
    t = f"svc_{uuid.uuid4().hex[:8]}"
    await set_service_state(db_pool, t, "deploying", set_by="ansible", now=LONG_AGO)
    out = await env.run(act.clear_converged_deploys, [])
    assert t in out["cleared"]
