"""SentryPollFlow tests."""

from __future__ import annotations

import asyncio

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.sentry_ingest import (
        FetchNewIssuesInput,
        FetchNewIssuesResult,
    )
    from aegis_worker.flows.sentry_poll import SentryPollFlow, SentryPollInput


# Stub AlertInvestigationFlow to avoid registering its many activities
@workflow.defn(name="AlertInvestigationFlow")
class _StubAlertInvestigationFlow:
    @workflow.run
    async def run(self, alert: dict) -> dict:
        return {"verdict": "ok", "fingerprint": alert.get("fingerprint", "")}


# Never-completing investigation — models AlertInvestigationFlow blocked on an
# un-answered Gate-2 approval. The poll must NOT wait for this; if it does, the
# workflow hangs and the test times out (the bug this flow fixes — 2026-05-29).
@workflow.defn(name="AlertInvestigationFlow")
class _BlockingAlertInvestigationFlow:
    @workflow.run
    async def run(self, alert: dict) -> dict:
        await workflow.wait_condition(lambda: False)  # blocks forever
        return {}


_calls: dict[str, list] = {"read_c": [], "fetch": [], "to_alert": [], "idem": [], "write_c": []}


@activity.defn(name="read_sentry_cursor")
async def stub_read_cursor() -> str | None:
    _calls["read_c"].append(1)
    return None


@activity.defn(name="fetch_new_issues")
async def stub_fetch(inp: FetchNewIssuesInput) -> FetchNewIssuesResult:
    _calls["fetch"].append(inp.since_issue_id)
    return FetchNewIssuesResult(
        issues=[
            {"id": "2", "title": "Error B", "level": "error", "project": {"slug": "app"}},
            {"id": "1", "title": "Error A", "level": "error", "project": {"slug": "app"}},
        ],
        latest_issue_id="2",
    )


@activity.defn(name="issue_to_alert")
async def stub_to_alert(issue: dict) -> dict:
    _calls["to_alert"].append(issue["id"])
    return {
        "source": "sentry",
        "title": issue["title"],
        "fingerprint": f"sentry:{issue['id']}",
        "severity": "error",
        "service": "app",
        "description": "",
        "labels": {},
        "raw_payload": issue,
    }


@activity.defn(name="ingest_idempotency_claim")
async def stub_idem(source_type: str, external_id: str) -> bool:
    _calls["idem"].append((source_type, external_id))
    return True


@activity.defn(name="write_sentry_cursor")
async def stub_write(issue_id: str) -> None:
    _calls["write_c"].append(issue_id)


ALL_STUBS = [stub_read_cursor, stub_fetch, stub_to_alert, stub_idem, stub_write]


def _reset():
    for v in _calls.values():
        v.clear()


@pytest.mark.asyncio
async def test_poll_mode_investigates_all():
    _reset()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[SentryPollFlow, _StubAlertInvestigationFlow],
            activities=ALL_STUBS,
        ),
    ):
        result = await env.client.execute_workflow(
            SentryPollFlow.run,
            SentryPollInput(mode="poll"),
            id="sp-1",
            task_queue="tq",
        )
    assert result["mode"] == "poll"
    assert result["polled"] == 2
    assert result["investigated"] == 2
    assert result["failed"] == 0
    assert _calls["write_c"] == ["2"]


@pytest.mark.asyncio
async def test_webhook_mode_single_issue():
    _reset()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[SentryPollFlow, _StubAlertInvestigationFlow],
            activities=ALL_STUBS,
        ),
    ):
        result = await env.client.execute_workflow(
            SentryPollFlow.run,
            SentryPollInput(
                mode="webhook",
                issue={
                    "id": "42",
                    "title": "Crash",
                    "level": "error",
                    "project": {"slug": "app"},
                },
            ),
            id="sp-2",
            task_queue="tq",
        )
    assert result["mode"] == "webhook"
    assert result["investigated"] == 1
    # Investigation is now fire-and-forget (ABANDON) — no verdict awaited.
    assert "verdict" not in result
    assert _calls["fetch"] == []  # poll activities never called
    assert _calls["write_c"] == []


@pytest.mark.asyncio
async def test_duplicate_issue_skipped():
    _reset()

    @activity.defn(name="ingest_idempotency_claim")
    async def dup(source_type: str, external_id: str) -> bool:
        return False

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[SentryPollFlow, _StubAlertInvestigationFlow],
            activities=[stub_read_cursor, stub_fetch, stub_to_alert, dup, stub_write],
        ),
    ):
        result = await env.client.execute_workflow(
            SentryPollFlow.run,
            SentryPollInput(mode="poll"),
            id="sp-3",
            task_queue="tq",
        )
    assert result["investigated"] == 0
    assert _calls["write_c"] == ["2"]  # cursor still advances


@pytest.mark.asyncio
async def test_poll_does_not_block_on_pending_investigation():
    """Regression (2026-05-29): a SentryPollFlow run was stuck for 41h because
    it awaited an AlertInvestigationFlow that was itself blocked on an
    un-answered Gate-2 approval. With OverlapPolicy=Skip on the schedule, 511
    subsequent ticks were skipped → total Sentry blindness.

    Fix: investigations spawn as ABANDONED children, never awaited. This test
    uses an investigation that blocks forever — the poll MUST still complete
    (dispatch-and-advance), proving it doesn't wait on Gate-2. If the poll
    awaited the child, this would hang and trip the timeout.
    """
    _reset()

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[SentryPollFlow, _BlockingAlertInvestigationFlow],
            activities=ALL_STUBS,
        ),
    ):
        result = await asyncio.wait_for(
            env.client.execute_workflow(
                SentryPollFlow.run,
                SentryPollInput(mode="poll"),
                id="sp-4",
                task_queue="tq",
            ),
            timeout=30,
        )
    # Both investigations were DISPATCHED (counted) even though neither will
    # ever complete, and the cursor advanced so the next poll moves forward.
    assert result["polled"] == 2
    assert result["investigated"] == 2
    assert result["failed"] == 0
    assert _calls["write_c"] == ["2"]
