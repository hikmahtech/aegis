"""GitHubAlertFlow tests — scoped pull-request notifier."""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

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


def _pr_payload(action: str) -> dict:
    return {
        "action": action,
        "repository": {"full_name": "youruser/aegis"},
        "pull_request": {
            "number": 42,
            "title": "Add streamlining",
            "user": {"login": "youruser"},
            "html_url": "https://github.com/youruser/aegis/pull/42",
        },
    }


async def _run(inp: GitHubAlertInput, stub, wid: str) -> dict:
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="tq", workflows=[GitHubAlertFlow], activities=[stub]),
    ):
        return await env.client.execute_workflow(
            GitHubAlertFlow.run, inp, id=wid, task_queue="tq"
        )


def test_pr_from_payload_extracts_fields():
    pr = _pr_from_payload(_pr_payload("opened"))
    assert pr["repo"] == "youruser/aegis"
    assert pr["number"] == 42
    assert pr["author"] == "youruser"
    assert pr["action"] == "opened"
    assert pr["url"].endswith("/pull/42")


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
