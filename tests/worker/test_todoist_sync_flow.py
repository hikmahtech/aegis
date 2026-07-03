"""Tests for TodoistSyncFlow — orchestration of bootstrap + sync + drain."""

from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

# ---------------------------------------------------------------------------
# Call-order tracker
# ---------------------------------------------------------------------------

_calls: list = []


def _reset(drain_failed: int = 0) -> None:
    global _drain_failed
    _calls.clear()
    _drain_failed = drain_failed


# ---------------------------------------------------------------------------
# Fake activities (module-level, matching TodoistActivities method signatures)
# ---------------------------------------------------------------------------


@activity.defn(name="bootstrap_if_empty")
async def fake_bootstrap_if_empty() -> dict:
    _calls.append("bootstrap")
    return {"bootstrapped": True}


@activity.defn(name="fetch_sync")
async def fake_fetch_sync() -> dict:
    _calls.append("fetch")
    return {"sync_token": "abc", "projects": [], "items": [], "labels": [], "full_sync": False}


@activity.defn(name="apply_sync_diff")
async def fake_apply_sync_diff(diff: dict) -> dict:
    _calls.append(("apply", diff.get("sync_token")))
    return {"projects_upserted": 0, "tasks_upserted": 0, "labels_upserted": 0}


_drain_failed = 0


@activity.defn(name="drain_outbox")
async def fake_drain_outbox() -> dict:
    _calls.append("drain")
    return {"committed": 0, "failed": _drain_failed}


@activity.defn(name="send_message")
async def fake_send_message(
    agent_id: str, message: str, chat_id: int = 0, keyboard: dict | None = None
) -> dict:
    _calls.append(("alert", agent_id, message))
    return {"ok": True}


ALL_FAKES = [
    fake_bootstrap_if_empty,
    fake_fetch_sync,
    fake_apply_sync_diff,
    fake_drain_outbox,
    fake_send_message,
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_flow_runs_bootstrap_then_sync_then_drain():
    _reset()
    from aegis_worker.flows.todoist_sync import TodoistSyncConfig, TodoistSyncFlow

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=f"tq-{uuid.uuid4()}",
            workflows=[TodoistSyncFlow],
            activities=ALL_FAKES,
        ) as worker,
    ):
        result = await env.client.execute_workflow(
            TodoistSyncFlow.run,
            TodoistSyncConfig(agent_id="sebas"),
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

    assert result["bootstrapped"] is True
    assert result["sync_token"] == "abc"
    # Order matters: bootstrap → fetch → apply → drain. No alert when failed=0.
    assert _calls == ["bootstrap", "fetch", ("apply", "abc"), "drain"]


@pytest.mark.asyncio
async def test_sync_flow_alerts_on_permanently_failed_outbox_commands():
    """failed>0 from drain_outbox means captured work was permanently lost —
    the flow must fire a chat alert (and still complete)."""
    _reset(drain_failed=2)
    from aegis_worker.flows.todoist_sync import TodoistSyncConfig, TodoistSyncFlow

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=f"tq-{uuid.uuid4()}",
            workflows=[TodoistSyncFlow],
            activities=ALL_FAKES,
        ) as worker,
    ):
        result = await env.client.execute_workflow(
            TodoistSyncFlow.run,
            TodoistSyncConfig(agent_id="sebas"),
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

    assert result["drained"]["failed"] == 2
    alerts = [c for c in _calls if isinstance(c, tuple) and c[0] == "alert"]
    assert len(alerts) == 1
    _, agent_id, message = alerts[0]
    assert agent_id == "sebas"
    assert "2 command(s)" in message
