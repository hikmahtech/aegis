"""HubActivities are thin wrappers; these pin the shapes, the no-pool path,
and the one seam every producer crosses (`ingest_alert`)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from aegis.services.hub import Event, get_problem, ingest_event, list_events, set_service_state
from aegis_worker.activities.hub import HubActivities
from temporalio.testing import ActivityEnvironment

pytestmark = pytest.mark.asyncio

# The activities run on the real clock, so a seeded window must be over in
# real time, not merely relative to a fixed test "now".
LONG_AGO = datetime(2026, 9, 7, 12, 0, tzinfo=UTC) - timedelta(days=30)


def _alert(subject: str, **kw) -> dict:
    return {
        "source": "aegis-heartbeat",
        "title": f"Service {subject} down",
        "fingerprint": f"aegis-heartbeat:DockerServiceDown:{subject}",
        "severity": "critical",
        "service": subject,
        "labels": {"alertname": "DockerServiceDown", "service_name": subject},
        **kw,
    }


async def test_no_pool_is_a_quiet_noop():
    env = ActivityEnvironment()
    act = HubActivities(db_pool=None)
    assert await env.run(act.promote_expired_suppressions) == {"promoted": 0, "problem_ids": []}
    assert await env.run(act.clear_converged_deploys, ["x"]) == {"cleared": []}
    assert await env.run(act.project_pending) == {"projected": 0, "created": 0, "errors": 0}
    ingested = await env.run(act.ingest_alert, _alert("s", todoist_task_id="T1"), False)
    assert ingested["investigate"] is True and ingested["todoist_task_id"] == "T1"
    assert (await env.run(act.ingest_alert, _alert("s"), True))["investigate"] is False
    assert (await env.run(act.problem_status, "p"))["found"] is False
    assert (await env.run(act.record_investigation, {"problem_id": "p", "status": "x", "text": "t"})) == {"recorded": False}
    assert (await env.run(act.mute_problem, "p", 24, "x")) == {"muted_until": None}
    assert await env.run(act.stale_stuck_problems, ["a"], 24.0) == []


async def test_ingest_alert_creates_then_attaches_and_resolves(db_pool):
    env = ActivityEnvironment()
    act = HubActivities(db_pool=db_pool)
    s = f"svc_{uuid.uuid4().hex[:8]}"
    first = await env.run(act.ingest_alert, _alert(s), False)
    assert first["action"] == "created" and first["investigate"] is True
    pid = first["problem_id"]
    # the same fingerprint again is a repeat occurrence: counted, not investigated
    second = await env.run(act.ingest_alert, _alert(s), False)
    assert second["problem_id"] == pid and second["investigate"] is False
    assert second["action"] in {"attached", "duplicate"}
    status = await env.run(act.problem_status, pid)
    assert status["found"] and status["status"] == "open" and status["resolved"] is False
    done = await env.run(act.ingest_alert, _alert(s), True)
    assert done["action"] == "resolved"
    assert (await env.run(act.problem_status, pid))["resolved"] is True


async def test_ingest_alert_adopts_the_callers_task(db_pool):
    env = ActivityEnvironment()
    act = HubActivities(db_pool=db_pool)
    s = f"svc_{uuid.uuid4().hex[:8]}"
    out = await env.run(act.ingest_alert, _alert(s, todoist_task_id="T_CLARIFY"), False)
    assert out["todoist_task_id"] == "T_CLARIFY"
    assert (await get_problem(db_pool, out["problem_id"]))["todoist_task_id"] == "T_CLARIFY"


async def test_record_investigation_moves_status_and_links_prs(db_pool):
    env = ActivityEnvironment()
    act = HubActivities(db_pool=db_pool)
    s = f"svc_{uuid.uuid4().hex[:8]}"
    pid = (await env.run(act.ingest_alert, _alert(s), False))["problem_id"]
    out = await env.run(
        act.record_investigation,
        {
            "problem_id": pid,
            "status": "fixing",
            "text": "PR opened",
            "external_id": "wf-1:prs_opened",
            "posted": True,
            "payload": {"pr_urls": ["https://github.com/o/r/pull/5"]},
        },
    )
    assert out == {"recorded": True, "status_changed": True}
    p = await get_problem(db_pool, pid)
    assert p["status"] == "fixing"
    ev = [e for e in await list_events(db_pool, pid) if e["kind"] == "investigation"][0]
    assert ev["payload"]["posted"] is True and ev["payload"]["status"] == "fixing"
    link = await db_pool.fetchval(
        "SELECT ref FROM problem_links WHERE problem_id = $1::uuid AND link_kind = 'github_pr'", pid
    )
    assert link == "https://github.com/o/r/pull/5"
    # idempotent on the external id
    again = await env.run(act.record_investigation, {"problem_id": pid, "status": "fixing", "text": "PR opened", "external_id": "wf-1:prs_opened"})
    assert again["status_changed"] is False


async def test_mute_and_verification_delay(db_pool):
    env = ActivityEnvironment()
    act = HubActivities(db_pool=db_pool)
    s = f"svc_{uuid.uuid4().hex[:8]}"
    pid = (await env.run(act.ingest_alert, _alert(s), False))["problem_id"]
    out = await env.run(act.mute_problem, pid, 24, "gate2")
    assert out["muted_until"]
    assert (await env.run(act.problem_status, pid))["muted"] is True
    assert await env.run(act.verification_delay, _alert(s)) == {"delay_seconds": 300}
    assert await env.run(act.verification_delay, {"labels": {"alertname": "HeartbeatCollectFailed"}}) == {"delay_seconds": 0}
    assert await env.run(act.verification_delay, {}) == {"delay_seconds": 180}


async def test_stale_stuck_problems_round_trip(db_pool):
    env = ActivityEnvironment()
    act = HubActivities(db_pool=db_pool)
    s = f"svc_{uuid.uuid4().hex[:8]}"
    r = await ingest_event(
        db_pool,
        Event(source="heartbeat", external_id=f"{s}@1", kind="occurrence", title="down", klass="DockerServiceDown", subject=s, occurred_at=LONG_AGO),
        now=LONG_AGO,
    )
    rows = await env.run(act.stale_stuck_problems, [s, "other"], 24.0)
    assert [x["id"] for x in rows] == [r.problem_id]
    assert rows[0]["hours"] > 24


async def test_promote_and_clear_round_trip(db_pool):
    s = f"svc_{uuid.uuid4().hex[:8]}"
    # A window that expired long ago, with a problem seen inside it.
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
