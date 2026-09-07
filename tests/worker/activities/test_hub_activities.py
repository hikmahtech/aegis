"""HubActivities are thin wrappers; these pin the shapes, the no-pool path,
and the one seam every producer crosses (`ingest_alert`)."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from aegis.services.hub import (
    Event,
    get_problem,
    ingest_event,
    list_events,
    set_service_state,
    set_status,
)
from aegis_worker.activities.hub import HubActivities
from temporalio.testing import ActivityEnvironment

pytestmark = pytest.mark.asyncio

# The activities run on the real clock, so a seeded window must be over in
# real time, not merely relative to a fixed test "now".
LONG_AGO = datetime(2026, 9, 7, 12, 0, tzinfo=UTC) - timedelta(days=30)


def _attempt(activity_id: str = "1", attempt: int = 1) -> ActivityEnvironment:
    """An environment whose `Info` is what a real one carries across the
    attempts of ONE activity task: same workflow id, same activity id, a
    higher attempt number."""
    env = ActivityEnvironment()
    env.info = dataclasses.replace(
        ActivityEnvironment.default_info(),
        workflow_id="hb-fixed",
        activity_id=activity_id,
        attempt=attempt,
    )
    return env


def _stale_occ(subject: str, klass: str) -> Event:
    return Event(
        source="heartbeat",
        external_id=f"{subject}:{klass}:{uuid.uuid4().hex[:8]}",
        kind="occurrence",
        title=f"{klass} on {subject}",
        klass=klass,
        subject=subject,
        severity="critical",
        occurred_at=LONG_AGO,
    )


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
    assert await env.run(act.record_plan, {"task_id": "T1", "steps": ["a", "b"]}) == {
        "recorded": False,
        "steps": 2,
    }
    assert await env.run(act.build_digest, 24.0) == {"message": "", "count": 0}
    assert await env.run(act.close_resolved_problems, 7.0) == {"closed": 0, "problem_ids": []}
    assert await env.run(act.stale_stuck_problems, ["a"], 24.0) == []
    out = await env.run(
        act.reconcile_findings,
        {"source": "drift", "subject_kind": "service", "classes": ["replicas"], "findings": [{"klass": "replicas", "subject": "s", "title": "t"}]},
    )
    assert out["fresh"][0]["problem_id"] is None and out["resolved"] == []


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


async def test_reconcile_findings_round_trip(db_pool):
    env = ActivityEnvironment()
    act = HubActivities(db_pool=db_pool)
    s = f"svc_{uuid.uuid4().hex[:8]}"
    inp = {
        "source": "drift",
        "subject_kind": "service",
        "classes": ["replicas", "oom_exit"],
        "findings": [{"klass": "replicas", "subject": s, "title": f"{s} replicas", "severity": "critical"}],
    }
    first = await env.run(act.reconcile_findings, inp)
    assert [f["subject"] for f in first["fresh"]] == [s]
    pid = first["fresh"][0]["problem_id"]
    assert (await get_problem(db_pool, pid))["severity"] == "critical"
    gone = await env.run(act.reconcile_findings, {**inp, "findings": []})
    assert [r["problem_id"] for r in gone["resolved"]] == [pid]


async def test_record_plan_gives_a_plain_task_a_problem_and_its_steps(db_pool):
    """A coding turn's plan lands on the task's problem, creating one when the
    task is a plain `@code` task nobody alerted about. The projector turns the
    steps into subtasks; here we pin the event, which is what it reads."""
    env = ActivityEnvironment()
    act = HubActivities(db_pool=db_pool)
    task = f"zzp-{uuid.uuid4().hex[:6]}"
    await db_pool.execute(
        "INSERT INTO todoist_tasks (id, content, labels, is_completed, updated_at) "
        "VALUES ($1, 'Fix the retry policy', ARRAY['@pandora','@code'], false, now())",
        task,
    )
    out = await env.run(
        act.record_plan,
        {"task_id": task, "steps": ["Add the index", "Backfill"], "text": "PLAN:\n1. ...",
         "external_id": f"plan:{task}:1"},
    )
    assert out["recorded"] is True and out["steps"] == 2
    p = await get_problem(db_pool, out["problem_id"])
    assert p["class"] == "manual" and p["todoist_task_id"] == task
    plans = [e for e in await list_events(db_pool, p["id"]) if e["kind"] == "plan"]
    assert len(plans) == 1
    assert plans[0]["payload"]["steps"] == ["Add the index", "Backfill"]
    assert plans[0]["payload"]["posted"] is True, "the turn already commented the plan"

    # Same external id, same turn: one plan, not two.
    again = await env.run(
        act.record_plan,
        {"task_id": task, "steps": ["Add the index", "Backfill"], "external_id": f"plan:{task}:1"},
    )
    assert again["recorded"] is True
    assert len([e for e in await list_events(db_pool, p["id"]) if e["kind"] == "plan"]) == 1


async def test_record_plan_needs_two_steps_and_a_known_task(db_pool):
    env = ActivityEnvironment()
    act = HubActivities(db_pool=db_pool)
    assert (await env.run(act.record_plan, {"task_id": "T", "steps": ["one"]}))["recorded"] is False
    assert (await env.run(act.record_plan, {"steps": ["a", "b"]}))["recorded"] is False
    missing = await env.run(
        act.record_plan, {"task_id": f"zz-gone-{uuid.uuid4().hex[:4]}", "steps": ["a", "b"]}
    )
    assert missing["recorded"] is False


async def test_build_digest_renders_what_the_window_saw(db_pool):
    """The briefing's message, straight from the events. No buffer to fill and
    none to clear, so asking twice gives the same answer — which is what makes
    a re-run of the briefing safe."""
    env = ActivityEnvironment()
    act = HubActivities(db_pool=db_pool)
    # Everything already in the shared test database is aged out of the window.
    await db_pool.execute(
        "UPDATE problem_events SET occurred_at = now() - interval '40 days' "
        "WHERE occurred_at > now() - interval '2 days'"
    )
    s = f"svc_{uuid.uuid4().hex[:8]}"
    await env.run(act.ingest_alert, _alert(s), False)

    out = await env.run(act.build_digest, 24.0)
    assert out["count"] == 1
    assert "<b>Problem digest</b> (last 24h)" in out["message"]
    assert "1 problems saw activity: 1 new" in out["message"]
    assert f"Service {s} down" in out["message"] and "🆕" in out["message"]
    assert out == await env.run(act.build_digest, 24.0), "asking twice is the same answer"

    quiet = await env.run(act.build_digest, 0.0)
    assert quiet == {"message": "", "count": 0}, "an empty window says nothing at all"


async def test_close_resolved_problems_sweeps_old_resolutions(db_pool):
    env = ActivityEnvironment()
    act = HubActivities(db_pool=db_pool)
    s = f"svc_{uuid.uuid4().hex[:8]}"
    ingested = await env.run(act.ingest_alert, _alert(s), False)
    pid = ingested["problem_id"]
    await env.run(act.ingest_alert, _alert(s), True)
    await db_pool.execute(
        "UPDATE problems SET resolved_at = now() - interval '30 days' WHERE id = $1::uuid", pid
    )

    out = await env.run(act.close_resolved_problems, 7.0)
    assert pid in out["problem_ids"] and out["closed"] >= 1
    assert (await get_problem(db_pool, pid))["status"] == "closed"
    assert pid not in (await env.run(act.close_resolved_problems, 7.0))["problem_ids"]
    assert (await env.run(act.close_resolved_problems, -1.0)) == {"closed": 0, "problem_ids": []}


# --- what the end-of-programme validation found (PR 8) ------------------------


async def test_a_retried_ingest_does_not_mint_a_second_occurrence(db_pool):
    """A heartbeat alert carries no timestamp of its own, so the occurrence id
    used to take the wall clock — and a Temporal RETRY of an ingest that had
    already committed minted a second occurrence, which attaches instead of
    creating and answers `investigate=False`. The alert then had a task and no
    investigation.

    Temporal keeps the activity id across attempts of one activity task, so
    that is what the id is derived from. Falsifiable: drop `occurrence_key` and
    the second attempt reports `attached` with `investigate` False.
    """
    act = HubActivities(db_pool=db_pool)
    s = f"svc_{uuid.uuid4().hex[:8]}"
    alert = _alert(s)

    # Two ATTEMPTS of one activity task: same workflow id, same activity id.
    first = await _attempt(attempt=1).run(act.ingest_alert, alert, False)
    retry = await _attempt(attempt=2).run(act.ingest_alert, alert, False)

    assert first["action"] == "created" and first["investigate"] is True
    assert retry["problem_id"] == first["problem_id"]
    assert retry["action"] == "duplicate"
    assert retry["investigate"] is False, "a retry must not start a second investigation"
    assert (await get_problem(db_pool, first["problem_id"]))["occurrences"] == 1

    # A genuinely NEW occurrence — a different activity task — still counts.
    again = await _attempt(activity_id="99").run(act.ingest_alert, alert, False)
    assert again["action"] == "attached"
    assert (await get_problem(db_pool, first["problem_id"]))["occurrences"] == 2


async def test_stale_stuck_problems_only_answers_about_the_classes_asked_for(db_pool):
    """The heartbeat means "this service is still down". Matching on the
    subject alone also returned that service's memory alert and any problem
    parked on a gate card, and each came back as a re-investigation."""
    env = ActivityEnvironment()
    act = HubActivities(db_pool=db_pool)
    s = f"svc_{uuid.uuid4().hex[:8]}"
    down = await ingest_event(db_pool, _stale_occ(s, "DockerServiceDown"), now=LONG_AGO)
    memory = await ingest_event(db_pool, _stale_occ(s, "HostOutOfMemory"), now=LONG_AGO)
    carded = await ingest_event(db_pool, _stale_occ(s, "ServiceDownProlonged"), now=LONG_AGO)
    await set_status(db_pool, carded.problem_id, "waiting_human", reason="gate 2 is open")

    ids = [
        r["id"]
        for r in await env.run(
            act.stale_stuck_problems, [s], 1.0, ["dockerservicedown", "servicedownprolonged"]
        )
    ]

    assert down.problem_id in ids
    assert memory.problem_id not in ids, "a different class is a different problem"
    assert carded.problem_id not in ids, "someone is answering its card"

    # No class list is still the old, wide question — for a caller that means it.
    wide = [r["id"] for r in await env.run(act.stale_stuck_problems, [s], 1.0, None)]
    assert memory.problem_id in wide
