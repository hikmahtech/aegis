"""The weekly journal backfill also files the journal prompt's answers whose
filing failed (vault record spec §3), behind a patch marker: a run already in
flight across the deploy finishes as recorded and still replays."""

from __future__ import annotations

from uuid import uuid4

import pytest
from aegis_worker.flows.notes_backfill import (
    PATCH_FILE_ANSWERS,
    NotesBackfillConfig,
    NotesBackfillFlow,
)
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker


def _stubs(seen: list):
    @activity.defn(name="notes_backfill_journal")
    async def notes_backfill_journal(limit, since_days, batch, agent_id) -> dict:
        seen.append(("journal", since_days))
        return {"status": "ok", "entries": 0}

    @activity.defn(name="notes_file_answers")
    async def notes_file_answers(since_days) -> dict:
        seen.append(("answers", since_days))
        return {"status": "ok", "answers": 1, "filed": 1, "failed": 0}

    return [notes_backfill_journal, notes_file_answers]


async def _run(env, seen):
    async with Worker(
        env.client, task_queue="nb-answers", workflows=[NotesBackfillFlow], activities=_stubs(seen)
    ):
        handle = await env.client.start_workflow(
            NotesBackfillFlow.run,
            NotesBackfillConfig(agent_id="sebas", since_days=14),
            id=f"nb-answers-{uuid4()}",
            task_queue="nb-answers",
        )
        return await handle.result(), await handle.fetch_history()


@pytest.mark.asyncio
async def test_the_weekly_backfill_also_files_the_journal_answers():
    seen: list = []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result, _ = await _run(env, seen)
    assert seen == [("journal", 14), ("answers", 14)]
    assert result == {
        "status": "ok",
        "entries": 0,
        "answers": {"status": "ok", "answers": 1, "filed": 1, "failed": 0},
    }


@pytest.mark.asyncio
async def test_a_backfill_in_flight_across_the_deploy_replays(monkeypatch):
    real = workflow.patched
    monkeypatch.setattr(
        workflow, "patched", lambda pid: False if pid == PATCH_FILE_ANSWERS else real(pid)
    )
    seen: list = []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result, history = await _run(env, seen)
    monkeypatch.undo()
    assert seen == [("journal", 14)] and "answers" not in result, (
        "the premise: this run is the code from before the sweep"
    )
    await Replayer(workflows=[NotesBackfillFlow]).replay_workflow(history)
