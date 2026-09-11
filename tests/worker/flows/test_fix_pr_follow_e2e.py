"""A fix PR from the Gate-2 card to a verified fix, end to end (#502).

The three flows run for real against the test database: AlertInvestigationFlow
opens the PR from the card ("Open PR(s)"), GitHubAlertFlow takes the merge
webhook, and HubSweepFlow settles it. Every hub activity is the real one; only
the investigation itself, the chat and Todoist sends, and the sweep's other
steps are stubbed. Todoist comments are caught at the projector's one door
(`hub_project._post_note`), so the test reads what the task would say.

Two endings: the alert stays clear for the window and the problem resolves,
closing its task; or the alert comes back after the grace and the problem
reopens, and the task says so.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from aegis.services import hub_project
from aegis.services.hub import get_problem
from aegis_worker.activities.hub import HubActivities
from aegis_worker.flows.alert_investigation import AlertInvestigationFlow
from aegis_worker.flows.github_alert import GitHubAlertFlow, GitHubAlertInput
from aegis_worker.flows.hub_sweep import HubSweepConfig, HubSweepFlow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.worker.flows import _alert_flow_harness as h

pytestmark = pytest.mark.asyncio

# Stubbed: the investigation, chat and the Todoist sends. Real: every hub step.
_REAL_HUB = {"ingest_alert", "problem_status", "record_investigation", "mute_problem"}


@activity.defn(name="promote_expired_suppressions")
async def _promote() -> dict:
    return {"promoted": 0, "problem_ids": []}


@activity.defn(name="reconcile_completed_tasks")
async def _reconcile() -> dict:
    return {"resolved": 0, "problem_ids": [], "tasks_reopened": 0}


@activity.defn(name="project_pending")
async def _project_pending() -> dict:
    return {"projected": 0, "created": 0, "errors": 0}


@activity.defn(name="find_group_candidates")
async def _find(min_members: int, hours: float) -> list[dict]:
    return []


def _activities(hub: HubActivities) -> list:
    stubs = [s for s in h.STUBS if s.__temporal_activity_definition.name not in _REAL_HUB]
    return [
        *stubs,
        hub.ingest_alert,
        hub.problem_status,
        hub.record_investigation,
        hub.mute_problem,
        hub.follow_fix_pr,
        hub.verify_fixes,
        _promote,
        _reconcile,
        _project_pending,
        _find,
    ]


async def _setup(db_pool, monkeypatch) -> dict:
    """A task, a Sentry-shaped alert on it, the card answered "Open PR(s)",
    and a fix branch to open the PR from."""
    said: list[str] = []

    async def post_note(pool, settings, task_id, text):
        said.append(text)
        return True

    monkeypatch.setattr(hub_project, "_post_note", post_note)
    subject = f"shop_{uuid.uuid4().hex[:8]}"
    task = f"zzf-{uuid.uuid4().hex[:8]}"
    await db_pool.execute(
        "INSERT INTO todoist_tasks (id, content, labels, is_completed, updated_at) "
        "VALUES ($1, 'Checkout timeouts', ARRAY['#alert','@pandora'], false, now())",
        task,
    )
    h.reset(answer={"value": "open_all_prs"}, pr_url=f"https://github.com/acme/shop/pull/{uuid.uuid4().int % 100000}")
    h.fix_branch()
    alert = h.app_alert(
        title=f"TimeoutError in {subject}",
        fingerprint=f"sentry:{subject}",
        service=subject,
        problem_id="",
        todoist_task_id=task,
    )
    return {"alert": alert, "task": task, "said": said, "url": h.S.pr_url}


def _merged(url: str) -> GitHubAlertInput:
    return GitHubAlertInput(
        event="pull_request",
        delivery_id=uuid.uuid4().hex,
        payload={
            "action": "closed",
            "repository": {"full_name": "acme/shop"},
            "pull_request": {
                "number": 7,
                "title": "AEGIS-proposed fix",
                "user": {"login": "aegis"},
                "html_url": url,
                "merged": True,
                "merged_at": datetime.now(UTC).isoformat(),
                "closed_at": datetime.now(UTC).isoformat(),
            },
        },
    )


async def _open_and_merge(env, tq: str, db_pool, case: dict) -> str:
    """Card → PR opened → merge webhook. Returns the problem id."""
    result = await env.client.execute_workflow(
        "AlertInvestigationFlow", case["alert"], id=f"e2e-{uuid.uuid4().hex[:8]}", task_queue=tq
    )
    pid = result["problem_id"]
    p = await get_problem(db_pool, pid)
    assert p["status"] == "fixing", "an opened fix PR leaves the problem fixing"
    assert p["todoist_task_id"] == case["task"]
    link = await db_pool.fetchval(
        "SELECT ref FROM problem_links WHERE problem_id = $1::uuid AND link_kind = 'github_pr'", pid
    )
    assert link == case["url"]
    assert [k["outcome"] for k in h.S.kg] == ["opened_pr"]

    closed = await env.client.execute_workflow(
        "GitHubAlertFlow", _merged(case["url"]), id=f"gh-{uuid.uuid4().hex[:8]}", task_queue=tq
    )
    assert closed == {"notified": False, "reason": "pr_closed", "followed": 1}
    assert (await get_problem(db_pool, pid))["status"] == "verifying"
    assert any("Fix PR merged" in t and case["url"] in t for t in case["said"])
    return pid


async def _age_the_merge(db_pool, pid: str, hours: float) -> None:
    """Let `hours` pass: the problem's whole timeline, the merge included,
    moves that far into the past."""
    await db_pool.execute(
        "UPDATE problem_events SET occurred_at = occurred_at - make_interval(secs => $2) "
        "WHERE problem_id = $1::uuid",
        pid,
        hours * 3600,
    )


async def _sweep(env, tq: str) -> dict:
    return await env.client.execute_workflow(
        HubSweepFlow.run, HubSweepConfig(), id=f"sweep-{uuid.uuid4().hex[:8]}", task_queue=tq
    )


async def test_a_merged_fix_that_holds_resolves_and_closes_its_task(db_pool, monkeypatch):
    case = await _setup(db_pool, monkeypatch)
    hub = HubActivities(db_pool=db_pool)
    tq = h.task_queue()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=tq,
            workflows=[AlertInvestigationFlow, h.FakeInteractionFlow, GitHubAlertFlow, HubSweepFlow],
            activities=_activities(hub),
        ),
    ):
        pid = await _open_and_merge(env, tq, db_pool, case)

        # Too soon: the sweep leaves it alone.
        await _age_the_merge(db_pool, pid, 2)
        assert (await _sweep(env, tq))["fix_resolved"] == 0
        assert (await get_problem(db_pool, pid))["status"] == "verifying"

        await _age_the_merge(db_pool, pid, 23)
        swept = await _sweep(env, tq)

    assert swept["fix_resolved"] >= 1
    p = await get_problem(db_pool, pid)
    assert p["status"] == "resolved"
    # The sweep's projection step is stubbed; the real projector is what the
    # next tick runs.
    await hub_project.project(db_pool, pid)
    assert any("stayed clear for 24h" in t for t in case["said"])
    assert any(t.startswith("✅ Resolved at") for t in case["said"])
    closed = await db_pool.fetchval("SELECT is_completed FROM todoist_tasks WHERE id = $1", case["task"])
    assert closed is True


async def test_a_merged_fix_the_alert_comes_back_to_reopens(db_pool, monkeypatch):
    case = await _setup(db_pool, monkeypatch)
    hub = HubActivities(db_pool=db_pool)
    tq = h.task_queue()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=tq,
            workflows=[AlertInvestigationFlow, h.FakeInteractionFlow, GitHubAlertFlow, HubSweepFlow],
            activities=_activities(hub),
        ),
    ):
        pid = await _open_and_merge(env, tq, db_pool, case)
        await _age_the_merge(db_pool, pid, 3)
        # The alert fires again, three hours after the merge.
        again = await hub.ingest_alert(case["alert"], False)
        assert again["problem_id"] == pid and again["investigate"] is False
        swept = await _sweep(env, tq)

    assert swept["fix_reopened"] >= 1
    assert (await get_problem(db_pool, pid))["status"] == "open"
    await hub_project.project(db_pool, pid)
    back = [t for t in case["said"] if "came back" in t]
    assert back and case["url"] in back[0] and "3.0h after the fix merged" in back[0]
    # The task was never closed, so there is nothing to reopen: it stays open.
    assert await db_pool.fetchval(
        "SELECT is_completed FROM todoist_tasks WHERE id = $1", case["task"]
    ) is False
