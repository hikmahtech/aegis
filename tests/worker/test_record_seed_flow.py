"""RecordSeedFlow (vault record spec §12): three drafters, each as its
capability's holder, then ONE message listing the drafts, and no cards."""

from __future__ import annotations

from uuid import uuid4

import pytest
from aegis_worker.flows.record_seed import RecordSeedConfig, RecordSeedFlow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


def _stubs(seen: list, *, money_raises=False, nothing=False):
    @activity.defn(name="resolve_agents")
    async def resolve_agents(tags) -> dict:
        return {"gtd": "zz-gtd", "finance": "zz-fin", "research": "zz-res"}

    @activity.defn(name="record_seed_general")
    async def record_seed_general(agent_id) -> dict:
        seen.append(("general", agent_id))
        return {"status": "exists", "written": []} if nothing else {"status": "written", "written": ["me/about.draft.md"]}

    @activity.defn(name="record_seed_money")
    async def record_seed_money(agent_id) -> dict:
        seen.append(("money", agent_id))
        if money_raises:
            raise RuntimeError("books unreadable")
        return {"status": "exists", "written": []} if nothing else {"status": "written", "written": ["me/money.draft.md"]}

    @activity.defn(name="record_seed_interests")
    async def record_seed_interests(agent_id) -> dict:
        seen.append(("interests", agent_id))
        return {"status": "empty", "reason": "no_theme_cited_two_sources", "written": []}

    @activity.defn(name="send_message")
    async def send_message(agent_id, message, chat_id=0, thread_ref=None, thread_overflow=False) -> dict:
        seen.append(("send", agent_id, message))
        return {"ok": True}

    return [resolve_agents, record_seed_general, record_seed_money, record_seed_interests, send_message]


async def _run(seen, **kw):
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="seed", workflows=[RecordSeedFlow], activities=_stubs(seen, **kw)),
    ):
        return await env.client.execute_workflow(
            RecordSeedFlow.run, RecordSeedConfig(), id=f"seed-{uuid4()}", task_queue="seed"
        )


@pytest.mark.asyncio
async def test_each_holder_drafts_and_one_message_lists_the_drafts():
    seen: list = []
    result = await _run(seen)
    assert ("general", "zz-gtd") in seen and ("money", "zz-fin") in seen and ("interests", "zz-res") in seen
    sends = [s for s in seen if s[0] == "send"]
    assert len(sends) == 1 and sends[0][1] == "zz-gtd"
    assert "me/about.draft.md" in sends[0][2] and "me/money.draft.md" in sends[0][2]
    assert result["written"] == ["me/about.draft.md", "me/money.draft.md"]


@pytest.mark.asyncio
async def test_a_failing_drafter_does_not_stop_the_others():
    seen: list = []
    result = await _run(seen, money_raises=True)
    assert result["drafters"]["money"]["status"] == "error"
    assert ("interests", "zz-res") in seen and result["written"] == ["me/about.draft.md"]


@pytest.mark.asyncio
async def test_no_draft_means_no_message():
    seen: list = []
    result = await _run(seen, nothing=True)
    assert result["written"] == [] and not [s for s in seen if s[0] == "send"]
