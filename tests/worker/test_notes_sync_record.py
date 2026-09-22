"""The record folder leaves the note index (vault record spec §5), and
NotesSyncFlow compiles the record behind a patch marker."""

from __future__ import annotations

from uuid import uuid4

import pytest
from aegis.services import vault_layout as vl
from aegis_worker.activities.notes import unindexed_prefixes
from aegis_worker.flows.notes_sync import PATCH_COMPILE_RECORD, NotesSyncConfig, NotesSyncFlow
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker


def test_the_index_drops_the_record_folder_and_the_questions():
    assert unindexed_prefixes(vl.DEFAULT_LAYOUT) == ("raphael/questions/", "me/")
    lay = vl.layout_from({"record": {"dir": "about-me"}})
    assert unindexed_prefixes(lay) == ("raphael/questions/", "about-me/")


def _stubs(seen: list):
    @activity.defn(name="notes_index_vault")
    async def notes_index_vault(max_files, index_max_chars) -> dict:
        seen.append("index")
        return {"status": "ok", "indexed": 0}

    @activity.defn(name="notes_compile_record")
    async def notes_compile_record() -> dict:
        seen.append("compile")
        return {"status": "off"}

    return [notes_index_vault, notes_compile_record]


async def _run(env, seen):
    async with Worker(env.client, task_queue="ns-record", workflows=[NotesSyncFlow], activities=_stubs(seen)):
        handle = await env.client.start_workflow(
            NotesSyncFlow.run, NotesSyncConfig(), id=f"ns-record-{uuid4()}", task_queue="ns-record"
        )
        return await handle.result(), await handle.fetch_history()


@pytest.mark.asyncio
async def test_the_hourly_sync_compiles_the_record_after_indexing():
    seen: list = []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result, _ = await _run(env, seen)
    assert seen == ["index", "compile"]
    assert result == {"status": "ok", "indexed": 0, "record": {"status": "off"}}


@pytest.mark.asyncio
async def test_a_sync_in_flight_across_the_deploy_replays(monkeypatch):
    real = workflow.patched
    monkeypatch.setattr(workflow, "patched", lambda pid: False if pid == PATCH_COMPILE_RECORD else real(pid))
    seen: list = []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result, history = await _run(env, seen)
    monkeypatch.undo()
    assert seen == ["index"] and "record" not in result, "the premise: the code from before the step"
    await Replayer(workflows=[NotesSyncFlow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_the_compile_activity_does_nothing_with_the_record_off(db_pool):
    from aegis_worker.activities.record import RecordActivities
    from temporalio.testing import ActivityEnvironment

    await db_pool.execute("DELETE FROM settings WHERE key = 'vault_layout'")
    vl.invalidate_cache()
    acts = RecordActivities(settings=None, db_pool=db_pool)
    assert await ActivityEnvironment().run(acts.notes_compile_record) == {"status": "off"}
