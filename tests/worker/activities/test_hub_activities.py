"""HubActivities: the worker's thin wrappers over the problem hub.

The alert seam (`ingest_alert`) and the investigation's activities left with
the infra lane (DevOps vertical, a2-devops). Four sweep activities stay one
release as no-ops for replay; they are pinned here."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from aegis.services.hub import (
    Event,
    get_problem,
    ingest_event,
    set_status,
)
from aegis.services.hub_project import link_task
from aegis_worker.activities.hub import HubActivities
from temporalio.testing import ActivityEnvironment

pytestmark = pytest.mark.asyncio


async def _open(db_pool, subject: str) -> str:
    """A live problem from one of v1's own producers."""
    now = datetime.now(UTC)
    r = await ingest_event(
        db_pool,
        Event(
            source="flow_health",
            external_id=f"{subject}@{uuid.uuid4().hex[:6]}",
            kind="occurrence",
            title=f"Flow {subject} failing",
            klass="flow_failing",
            subject=subject,
            subject_kind="flow",
            occurred_at=now,
        ),
        now=now,
    )
    return r.problem_id


async def test_no_pool_is_a_quiet_noop():
    env = ActivityEnvironment()
    act = HubActivities(db_pool=None)
    assert await env.run(act.reconcile_completed_tasks) == {
        "resolved": 0,
        "problem_ids": [],
        "tasks_reopened": 0,
    }
    assert await env.run(act.project_pending) == {"projected": 0, "created": 0, "errors": 0}
    assert await env.run(act.build_digest, 24.0) == {"message": "", "count": 0}
    assert await env.run(act.close_resolved_problems, 7.0) == {"closed": 0, "problem_ids": []}
    out = await env.run(
        act.reconcile_findings,
        {"source": "flow_health", "subject_kind": "flow", "classes": ["flow_failing"], "findings": [{"klass": "flow_failing", "subject": "s", "title": "t"}]},
    )
    assert out["fresh"][0]["problem_id"] is None and out["resolved"] == []


async def test_the_retired_sweep_activities_are_no_ops():
    """Kept one release so a sweep recorded before `PATCH_DROP_INFRA_STEPS` (and
    `PATCH_DROP_FIX_VERIFICATION`) still finds them; each does nothing."""
    env = ActivityEnvironment()
    act = HubActivities(db_pool=None)
    assert await env.run(act.promote_expired_suppressions) == {"promoted": 0, "problem_ids": []}
    assert await env.run(act.promoted_investigations, ["a"]) == []
    assert await env.run(act.reconcile_alertmanager, "http://am:9093", 900) == {
        "resolved": 0,
        "checked": 0,
        "skipped": "retired",
    }
    assert await env.run(act.retire_cards, {}) == {"retired": 0, "finished": 0}
    assert await env.run(act.verify_fixes, 24.0, 1.0) == {
        "resolved": 0,
        "reopened": 0,
        "problem_ids": [],
    }


async def test_reconcile_completed_tasks_round_trip(db_pool):
    """The sweep's step 2 on its real path: a task a person ticked off in
    Todoist (the sync mirrors it `is_completed`) resolves its problem, and a
    retry of the activity changes nothing more."""
    env = ActivityEnvironment()
    act = HubActivities(db_pool=db_pool)
    s = f"svc_{uuid.uuid4().hex[:8]}"
    task = f"zzc-{uuid.uuid4().hex[:8]}"
    await db_pool.execute(
        "INSERT INTO todoist_tasks (id, content, labels, is_completed, updated_at) "
        "VALUES ($1, 'down', ARRAY['#alert','@sebas'], false, now())",
        task,
    )
    pid = await _open(db_pool, s)
    await link_task(db_pool, pid, task)
    await set_status(db_pool, pid, "waiting_human", reason="waiting on a person")
    assert pid not in (await env.run(act.reconcile_completed_tasks))["problem_ids"]

    await db_pool.execute("UPDATE todoist_tasks SET is_completed = true WHERE id = $1", task)
    out = await env.run(act.reconcile_completed_tasks)
    assert pid in out["problem_ids"] and out["resolved"] >= 1
    assert (await get_problem(db_pool, pid))["status"] == "resolved"
    assert pid not in (await env.run(act.reconcile_completed_tasks))["problem_ids"]


async def test_reconcile_findings_round_trip(db_pool):
    env = ActivityEnvironment()
    act = HubActivities(db_pool=db_pool)
    s = f"svc_{uuid.uuid4().hex[:8]}"
    inp = {
        "source": "flow_health",
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
    await _open(db_pool, s)

    out = await env.run(act.build_digest, 24.0)
    assert out["count"] == 1
    assert "<b>Problem digest</b> (last 24h)" in out["message"]
    assert "1 problems saw activity: 1 new" in out["message"]
    assert f"Flow {s} failing" in out["message"] and "🆕" in out["message"]
    assert out == await env.run(act.build_digest, 24.0), "asking twice is the same answer"

    quiet = await env.run(act.build_digest, 0.0)
    assert quiet == {"message": "", "count": 0}, "an empty window says nothing at all"


async def test_close_resolved_problems_sweeps_old_resolutions(db_pool):
    env = ActivityEnvironment()
    act = HubActivities(db_pool=db_pool)
    s = f"svc_{uuid.uuid4().hex[:8]}"
    pid = await _open(db_pool, s)
    await set_status(db_pool, pid, "resolved", reason="test")
    await db_pool.execute(
        "UPDATE problems SET resolved_at = now() - interval '30 days' WHERE id = $1::uuid", pid
    )

    out = await env.run(act.close_resolved_problems, 7.0)
    assert pid in out["problem_ids"] and out["closed"] >= 1
    assert (await get_problem(db_pool, pid))["status"] == "closed"
    assert pid not in (await env.run(act.close_resolved_problems, 7.0))["problem_ids"]
    assert (await env.run(act.close_resolved_problems, -1.0)) == {"closed": 0, "problem_ids": []}
