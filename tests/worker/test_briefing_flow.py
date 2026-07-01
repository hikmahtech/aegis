"""DailyBriefingFlow: gather_changes → frame → send → commit (after send)."""
from __future__ import annotations

import uuid

import pytest
from aegis_worker.flows.daily_briefing import DailyBriefingConfig, DailyBriefingFlow
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


def _stubs(sent, committed, fail_send=False):
    @activity.defn(name="send_system_event")
    async def sys_evt(msg):
        return {"ok": True}

    @activity.defn(name="gather_briefing_changes")
    async def gather():
        return {"quiet": False, "intel": [], "broke": {"failed_runs": [], "new_drift": []},
                "knowledge": {"contradictions_delta": 0, "contradictions_total": 0, "top": []},
                "calendar": {"today": [], "new_ids": []},
                "_new_state": {"last_briefing_at": "2026-06-23T00:00:00+00:00"}}

    @activity.defn(name="frame_briefing")
    async def frame(changes):
        return "narrative body"

    @activity.defn(name="send_telegram")
    async def send_tg(agent_id, message, chat_id=0, keyboard=None):
        if fail_send and agent_id != "pandoras-actor":
            raise RuntimeError("telegram down")
        sent.append((agent_id, message))
        return {"ok": True}

    @activity.defn(name="build_alert_digest")
    async def digest():
        return {"count": 0}

    @activity.defn(name="ingest_briefing")
    async def ingest(text, date):
        return True

    @activity.defn(name="commit_briefing_state")
    async def commit(state):
        committed.append(state)

    return [sys_evt, gather, frame, send_tg, digest, ingest, commit]


@pytest.mark.asyncio
async def test_flow_sends_and_commits():
    sent, committed = [], []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client: Client = env.client
        async with Worker(client, task_queue="brf-test", workflows=[DailyBriefingFlow],
                          activities=_stubs(sent, committed)):
            res = await client.execute_workflow(
                DailyBriefingFlow.run, DailyBriefingConfig(agent_id="raphael"),
                id=f"brf-{uuid.uuid4()}", task_queue="brf-test")
    assert res["status"] == "delivered"
    assert any(a == "raphael" and "narrative body" in m for a, m in sent)
    assert len(committed) == 1  # committed after a successful send


@pytest.mark.asyncio
async def test_flow_skips_commit_when_send_fails():
    sent, committed = [], []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = env.client
        async with Worker(client, task_queue="brf-test2", workflows=[DailyBriefingFlow],
                          activities=_stubs(sent, committed, fail_send=True)):
            res = await client.execute_workflow(
                DailyBriefingFlow.run, DailyBriefingConfig(agent_id="raphael"),
                id=f"brf-{uuid.uuid4()}", task_queue="brf-test2")
    assert res["status"] == "delivered"
    assert committed == []  # send failed → cursor not advanced
