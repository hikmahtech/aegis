"""GitHubAlertFlow tests — scoped pull-request notifier, and the close of a
fix PR an investigation opened (#502)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.github_alert import (
        GitHubAlertFlow,
        GitHubAlertInput,
        _pr_from_payload,
    )


_notify_calls: list[dict] = []


@activity.defn(name="notify_pr_event")
async def stub_notify_tracked(pr: dict) -> dict:
    _notify_calls.append(pr)
    return {"notified": True, "repo": pr.get("repo")}


@activity.defn(name="notify_pr_event")
async def stub_notify_untracked(pr: dict) -> dict:
    _notify_calls.append(pr)
    return {"notified": False, "reason": "untracked_repo", "repo": pr.get("repo")}


_followed: list[dict] = []


@activity.defn(name="follow_fix_pr")
async def stub_follow_fix_pr(pr: dict) -> dict:
    _followed.append(pr)
    return {
        "followed": 1,
        "problems": [{"problem_id": "p1", "state": "merged", "status": "verifying", "moved": True}],
    }


def _pr_payload(action: str, *, merged: bool = False) -> dict:
    return {
        "action": action,
        "repository": {"full_name": "youruser/aegis"},
        "pull_request": {
            "number": 42,
            "title": "Add streamlining",
            "user": {"login": "youruser"},
            "html_url": "https://github.com/youruser/aegis/pull/42",
            "merged": merged,
            "merged_at": "2026-09-12T10:00:00Z" if merged else None,
            "closed_at": "2026-09-12T10:00:00Z" if action == "closed" else None,
        },
    }


async def _run(inp: GitHubAlertInput, stub, wid: str, workflows: tuple = (GitHubAlertFlow,)) -> dict:
    result, _ = await _run_with_history(inp, [stub], wid, workflows)
    return result


async def _run_with_history(
    inp: GitHubAlertInput, activities: list, wid: str, workflows: tuple = (GitHubAlertFlow,)
):
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="tq", workflows=list(workflows), activities=activities),
    ):
        handle = await env.client.start_workflow("GitHubAlertFlow", inp, id=wid, task_queue="tq")
        return await handle.result(), await handle.fetch_history()


def test_pr_from_payload_extracts_fields():
    pr = _pr_from_payload(_pr_payload("opened"))
    assert pr["repo"] == "youruser/aegis"
    assert pr["number"] == 42
    assert pr["author"] == "youruser"
    assert pr["action"] == "opened"
    assert pr["url"].endswith("/pull/42")
    assert pr["merged"] is False

    closed = _pr_from_payload(_pr_payload("closed", merged=True))
    assert closed["merged"] is True
    assert closed["merged_at"] == closed["closed_at"] == "2026-09-12T10:00:00Z"


@pytest.mark.asyncio
async def test_pr_opened_on_tracked_repo_notifies():
    _notify_calls.clear()
    result = await _run(
        GitHubAlertInput(event="pull_request", delivery_id="d1", payload=_pr_payload("opened")),
        stub_notify_tracked,
        "gh-open",
    )
    assert result["notified"] is True
    assert len(_notify_calls) == 1
    assert _notify_calls[0]["repo"] == "youruser/aegis"


@pytest.mark.asyncio
async def test_pr_opened_on_untracked_repo_is_skipped_by_activity():
    _notify_calls.clear()
    result = await _run(
        GitHubAlertInput(event="pull_request", delivery_id="d2", payload=_pr_payload("opened")),
        stub_notify_untracked,
        "gh-untracked",
    )
    assert result["notified"] is False
    assert result["reason"] == "untracked_repo"


@pytest.mark.asyncio
async def test_pr_synchronize_is_filtered_before_activity():
    """Every-push 'synchronize' is excluded — the activity must not be called."""
    _notify_calls.clear()
    result = await _run(
        GitHubAlertInput(event="pull_request", delivery_id="d3", payload=_pr_payload("synchronize")),
        stub_notify_tracked,
        "gh-sync",
    )
    assert result == {"notified": False, "reason": "filtered"}
    assert _notify_calls == []


@pytest.mark.asyncio
async def test_non_pr_events_filtered():
    """workflow_run / push / issues no longer trigger anything."""
    for event, payload, wid in [
        ("workflow_run", {"repository": {"full_name": "org/repo"}}, "gh-wr"),
        ("push", {"repository": {"full_name": "org/repo"}}, "gh-push"),
        ("issues", {"action": "opened", "repository": {"full_name": "org/repo"}}, "gh-iss"),
    ]:
        _notify_calls.clear()
        result = await _run(
            GitHubAlertInput(event=event, delivery_id=wid, payload=payload),
            stub_notify_tracked,
            wid,
        )
        assert result == {"notified": False, "reason": "filtered"}
        assert _notify_calls == []


# --- a closed PR: the fix-PR follow-up (#502) ---------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("merged", [True, False])
async def test_a_closed_pr_goes_to_the_hub(merged):
    """Merged or not, a closed PR is handed to the hub, which knows whether an
    investigation opened it. Nothing is sent to chat: the task hears it."""
    _followed.clear()
    _notify_calls.clear()
    result, _ = await _run_with_history(
        GitHubAlertInput(
            event="pull_request", delivery_id="d4", payload=_pr_payload("closed", merged=merged)
        ),
        [stub_follow_fix_pr, stub_notify_tracked],
        f"gh-closed-{merged}",
    )
    assert result == {"notified": False, "reason": "pr_closed", "followed": 1}
    assert [(p["url"], p["merged"]) for p in _followed] == [
        ("https://github.com/youruser/aegis/pull/42", merged)
    ]
    assert _notify_calls == []


@workflow.defn(name="GitHubAlertFlow")
class _GitHubAlertFlowBefore502:
    """GitHubAlertFlow as it was before #502: a closed PR was filtered out
    without any command. Kept so a history it wrote replays against today's
    flow."""

    @workflow.run
    async def run(self, input: GitHubAlertInput) -> dict:
        if input.event != "pull_request" or input.payload.get("action") not in {
            "opened",
            "reopened",
            "ready_for_review",
        }:
            return {"notified": False, "reason": "filtered"}
        return await workflow.execute_activity(
            "notify_pr_event",
            args=[_pr_from_payload(input.payload)],
            start_to_close_timeout=timedelta(seconds=15),
        )


@pytest.mark.asyncio
async def test_a_close_filtered_before_the_deploy_replays_on_the_new_worker():
    """The follow-up is behind `workflow.patched`: a closed-PR run the old
    worker filtered replays through the new flow without the activity.

    Falsifiable: drop the guard and the replay raises a nondeterminism error.
    """
    inp = GitHubAlertInput(event="pull_request", delivery_id="d5", payload=_pr_payload("closed", merged=True))
    result, history = await _run_with_history(
        inp, [stub_follow_fix_pr], "gh-before", workflows=(_GitHubAlertFlowBefore502,)
    )
    assert result == {"notified": False, "reason": "filtered"}
    await Replayer(workflows=[GitHubAlertFlow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_the_new_flow_replays_its_own_close():
    inp = GitHubAlertInput(event="pull_request", delivery_id="d6", payload=_pr_payload("closed", merged=True))
    _, history = await _run_with_history(inp, [stub_follow_fix_pr], "gh-after")
    await Replayer(workflows=[GitHubAlertFlow]).replay_workflow(history)
