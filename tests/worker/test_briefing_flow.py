"""DailyBriefingFlow: gather_changes → frame → deliver → commit (after send)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from aegis_worker.activities.briefing import BriefingActivities
from aegis_worker.flows.daily_briefing import DailyBriefingConfig, DailyBriefingFlow
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


def _stubs(sent, committed, fail_send=False, resolve_map=None, market_calls=None):
    seed = (
        resolve_map if resolve_map is not None else {"finance": "maou", "infra": "pandoras-actor"}
    )

    @activity.defn(name="resolve_agents")
    async def resolve_agents(tags):
        return {t: seed.get(t) for t in tags}

    @activity.defn(name="gather_market_data")
    async def gather_market():
        if market_calls is not None:
            market_calls.append(True)
        return {"available": False, "indices": []}

    @activity.defn(name="send_system_event")
    async def sys_evt(msg):
        return {"ok": True}

    @activity.defn(name="gather_briefing_changes")
    async def gather():
        return {
            "quiet": False,
            "intel": [],
            "broke": {"failed_runs": [], "new_drift": []},
            "knowledge": {"contradictions_delta": 0, "contradictions_total": 0, "top": []},
            "calendar": {"today": [], "new_ids": []},
            "_new_state": {"last_briefing_at": "2026-06-23T00:00:00+00:00"},
        }

    @activity.defn(name="frame_briefing")
    async def frame(changes):
        return "narrative body"

    @activity.defn(name="deliver_briefing")
    async def deliver(agent_id, message):
        if fail_send:
            raise RuntimeError("comms down")
        sent.append((agent_id, message))
        return {"ok": True}

    @activity.defn(name="send_message")
    async def send_tg(agent_id, message, chat_id=0, keyboard=None):
        if fail_send and agent_id != "pandoras-actor":
            raise RuntimeError("comms down")
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

    return [
        resolve_agents, gather_market, sys_evt, gather, frame, deliver, send_tg,
        digest, ingest, commit,
    ]


@pytest.mark.asyncio
async def test_flow_sends_and_commits():
    sent, committed = [], []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client: Client = env.client
        async with Worker(
            client,
            task_queue="brf-test",
            workflows=[DailyBriefingFlow],
            activities=_stubs(sent, committed),
        ):
            res = await client.execute_workflow(
                DailyBriefingFlow.run,
                DailyBriefingConfig(agent_id="raphael"),
                id=f"brf-{uuid.uuid4()}",
                task_queue="brf-test",
            )
    assert res["status"] == "delivered"
    assert any(a == "raphael" and "narrative body" in m for a, m in sent)
    assert len(committed) == 1  # committed after a successful send


@pytest.mark.asyncio
async def test_flow_skips_commit_when_send_fails():
    sent, committed = [], []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = env.client
        async with Worker(
            client,
            task_queue="brf-test2",
            workflows=[DailyBriefingFlow],
            activities=_stubs(sent, committed, fail_send=True),
        ):
            res = await client.execute_workflow(
                DailyBriefingFlow.run,
                DailyBriefingConfig(agent_id="raphael"),
                id=f"brf-{uuid.uuid4()}",
                task_queue="brf-test2",
            )
    assert res["status"] == "delivered"
    assert committed == []  # send failed → cursor not advanced


# ── Issue #36: market section gated on the `finance` tag, not `== "maou"` ──


# ── Issue #215: the rendered health block must not reach workflow history ──


class _Recorder:
    """Stands in for DeliveryActivities — records what actually went out."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, agent_id, message, chat_id=0):
        self.sent.append((agent_id, message))
        return {"ok": True}


@pytest.mark.asyncio
async def test_the_health_block_never_enters_workflow_history(db_pool):
    """The claim in #215, asserted against REAL Temporal history bytes.

    Every activity here is a stub except `deliver_briefing`, which is the real
    one: it reads `life.observations`, renders the `<b>Health</b>` block and
    sends it, all inside the activity. So the reading exists, is rendered, and
    reaches the owner's channel — and none of it may appear in the history the
    Temporal web UI serves.

    Scanning the serialized history events catches every route in one go: the
    activity's own argument and result, `send_voice`'s argument, and the text
    handed to `ingest_briefing` (which in production is embedded into the
    knowledge store).
    """
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM life.observations WHERE source = 'health'")
        await conn.execute(
            "INSERT INTO life.observations (source, metric, value, observed_at, external_id) "
            "VALUES ('health', 'resting_hr', 54321.5, $1, 'zz215-hr')",
            datetime.now(UTC) - timedelta(hours=6),
        )

    rec = _Recorder()
    act = BriefingActivities(db_pool=db_pool, delivery=rec)
    sent, committed = [], []
    acts = [
        a
        for a in _stubs(sent, committed)
        if activity._Definition.must_from_callable(a).name != "deliver_briefing"
    ] + [act.deliver_briefing]

    wid = f"brf-{uuid.uuid4()}"
    try:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client, task_queue="brf-health", workflows=[DailyBriefingFlow], activities=acts
            ):
                await env.client.execute_workflow(
                    DailyBriefingFlow.run,
                    DailyBriefingConfig(agent_id="raphael"),
                    id=wid,
                    task_queue="brf-health",
                )

            blob = b""
            async for event in env.client.get_workflow_handle(wid).fetch_history_events():
                blob += event.SerializeToString()
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM life.observations WHERE external_id = 'zz215-hr'")

    # Non-vacuity, twice over: the reading is live and the block really was
    # rendered and delivered, and the history really does hold this run's text.
    assert len(rec.sent) == 1, "deliver_briefing never sent"
    assert "<b>Health</b>: resting_hr 54321.5" in rec.sent[0][1]
    assert b"narrative body" in blob, "the history scan found none of the briefing"

    assert b"54321" not in blob
    assert b"resting_hr" not in blob
    assert b"Health" not in blob


@pytest.mark.asyncio
async def test_market_section_runs_for_finance_tag_holder():
    """The agent that holds `finance` (here a renamed 'money-agent') gets the
    market section — gating is by tag, not by the literal id 'maou'."""
    sent, committed, market = [], [], []
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="brf-fin",
            workflows=[DailyBriefingFlow],
            activities=_stubs(
                sent, committed, resolve_map={"finance": "money-agent"}, market_calls=market
            ),
        ),
    ):
        await env.client.execute_workflow(
            DailyBriefingFlow.run,
            DailyBriefingConfig(agent_id="money-agent"),
            id=f"brf-{uuid.uuid4()}",
            task_queue="brf-fin",
        )
    assert market == [True]  # gather_market_data was called for the finance holder


@pytest.mark.asyncio
async def test_market_section_skipped_for_non_finance_agent():
    """A non-finance agent never triggers the market section, even though the
    finance tag resolves to someone else."""
    sent, committed, market = [], [], []
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="brf-nofin",
            workflows=[DailyBriefingFlow],
            activities=_stubs(
                sent, committed, resolve_map={"finance": "money-agent"}, market_calls=market
            ),
        ),
    ):
        await env.client.execute_workflow(
            DailyBriefingFlow.run,
            DailyBriefingConfig(agent_id="sebas"),
            id=f"brf-{uuid.uuid4()}",
            task_queue="brf-nofin",
        )
    assert market == []  # not the finance holder → no market data
