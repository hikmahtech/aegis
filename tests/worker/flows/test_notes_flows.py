"""NotesWriteFlow and NotesSyncFlow (#514)."""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.notes_backfill import NotesBackfillConfig, NotesBackfillFlow
    from aegis_worker.flows.notes_sync import NotesSyncConfig, NotesSyncFlow
    from aegis_worker.flows.notes_write import NotesWriteFlow, NotesWriteInput

_sent: list = []


@activity.defn(name="notes_write")
async def stub_write(op: str, payload: dict, agent_id: str = "") -> dict:
    return {"ok": True, "message": f"wrote to {payload['path']} as {agent_id}"}


@activity.defn(name="resolve_agents")
async def stub_resolve(tags: list[str]) -> dict:
    return dict.fromkeys(tags, "holder")


@activity.defn(name="send_message")
async def stub_send(agent_id: str, text: str) -> dict:
    _sent.append((agent_id, text))
    return {"ok": True}


async def _write(reply_after: int) -> dict:
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="tq", workflows=[NotesWriteFlow],
               activities=[stub_write, stub_send]),
    ):
        return await env.client.execute_workflow(
            NotesWriteFlow.run,
            NotesWriteInput(agent_id="raphael", op="write", payload={"path": "raphael/x.md"},
                            reply_after_seconds=reply_after),
            id=f"nw-{reply_after}",
            task_queue="tq",
        )


@pytest.mark.asyncio
async def test_a_write_the_tool_stopped_waiting_for_reports_itself():
    _sent.clear()
    out = await _write(0)
    assert out["status"] == "ok" and out["notified"] is True
    assert _sent and _sent[0][0] == "raphael" and "wrote to raphael/x.md as raphael" in _sent[0][1]


@pytest.mark.asyncio
async def test_a_write_inside_the_wait_is_not_reported_twice():
    _sent.clear()
    out = await _write(1000)
    assert out["message"] == "wrote to raphael/x.md as raphael" and out["notified"] is False
    assert _sent == []


@pytest.mark.asyncio
async def test_the_sync_flow_passes_its_batch_size_and_index_cut():
    @activity.defn(name="notes_index_vault")
    async def stub_index(max_files: int, index_max_chars: int = 0) -> dict:
        return {"status": "ok", "max_files": max_files, "index_max_chars": index_max_chars}

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="tq", workflows=[NotesSyncFlow], activities=[stub_index]),
    ):
        out = await env.client.execute_workflow(
            NotesSyncFlow.run, NotesSyncConfig(max_files=42, index_max_chars=5000),
            id="ns-1", task_queue="tq",
        )
    assert out == {"status": "ok", "max_files": 42, "index_max_chars": 5000}


@pytest.mark.asyncio
async def test_the_backfill_flow_passes_its_limit_window_batch_and_owner():
    @activity.defn(name="notes_backfill_journal")
    async def stub_backfill(
        limit: int, since_days: int = 0, batch: int = 50, agent_id: str = ""
    ) -> dict:
        return {"status": "ok", "limit": limit, "since_days": since_days, "batch": batch,
                "agent_id": agent_id}

    @activity.defn(name="notes_file_answers")
    async def stub_answers(since_days: int = 0) -> dict:
        return {"status": "ok", "answers": 0, "filed": 0, "failed": 0}

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="tq", workflows=[NotesBackfillFlow],
               activities=[stub_backfill, stub_answers, stub_resolve]),
    ):
        out = await env.client.execute_workflow(
            NotesBackfillFlow.run,
            NotesBackfillConfig(agent_id="raphael", limit=7, since_days=14, batch=3),
            id="nb-1", task_queue="tq",
        )
        by_hand = await env.client.execute_workflow(
            NotesBackfillFlow.run, NotesBackfillConfig(), id="nb-2", task_queue="tq"
        )
    assert out == {
        "status": "ok", "limit": 7, "since_days": 14, "batch": 3, "agent_id": "raphael",
        "answers": {"status": "ok", "answers": 0, "filed": 0, "failed": 0},
    }
    assert by_hand["since_days"] == 0, "a run started by hand takes every row"
    assert by_hand["batch"] == 50
    assert by_hand["agent_id"] == "holder", "no agent named: the research holder signs"
