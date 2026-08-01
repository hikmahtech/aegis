"""ExpiryRadarFlow — daily sweep over life.expiring_items into ack cards.

Runs the REAL `ExpiringItemsActivities` against real Postgres (the dedup ledger
is the thing under test; a stubbed claim activity would assert nothing) and the
REAL `InteractionFlow` as an abandoned child, with only the comms-facing
interaction activities stubbed.

Date sensitivity: every date comparison happens in Postgres (`due_within` uses
CURRENT_DATE), so the Temporal test server's freely-skipping clock cannot move
the window under us. The abandoned child, however, IS clock-sensitive — it
parks on a week-long timeout — so the polls that wait for its card run inside
`env.auto_time_skipping_disabled()`, otherwise the server races the clock past
the timeout instead of letting the worker run the child's first task.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from aegis.db import run_migrations
from aegis.services import expiring_items as svc
from aegis_worker.activities.expiring_items import ExpiringItemsActivities
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.interactions import (
        ApplyTimeoutInput,
        InsertInteractionInput,
        InsertInteractionResult,
        ResolveInteractionInput,
    )
    from aegis_worker.flows.expiry_radar import ExpiryRadarConfig, ExpiryRadarFlow
    from aegis_worker.flows.interaction import InteractionFlow

PREFIX = "zzradarflow-"
AGENT = "zzradarflow-agent"

_cards: list[dict] = []


@activity.defn(name="insert_interaction")
async def _stub_insert(input: InsertInteractionInput) -> InsertInteractionResult:
    return InsertInteractionResult(interaction_id=str(uuid4()))


@activity.defn(name="send_interaction_card")
async def _stub_send_card(
    interaction_id: str,
    agent_id: str,
    kind: str,
    prompt: str,
    options,
    allow_hint: bool = False,
) -> dict:
    _cards.append({"agent_id": agent_id, "kind": kind, "prompt": prompt})
    return {"ok": True}


@activity.defn(name="resolve_interaction")
async def _stub_resolve(input: ResolveInteractionInput) -> None:
    return None


@activity.defn(name="apply_interaction_timeout")
async def _stub_timeout(input: ApplyTimeoutInput) -> None:
    return None


async def _wipe(pool: asyncpg.Pool) -> None:
    # Children before parents: the alert ledger is a hard FK onto the items.
    await pool.execute(
        "DELETE FROM life.expiring_item_alerts WHERE item_id IN "
        "(SELECT id FROM life.expiring_items WHERE title LIKE $1)",
        f"{PREFIX}%",
    )
    await pool.execute("DELETE FROM life.expiring_items WHERE title LIKE $1", f"{PREFIX}%")
    await pool.execute("DELETE FROM notification_log WHERE agent_id = $1", AGENT)


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await run_migrations(db_pool)
    await _wipe(db_pool)
    _cards.clear()
    yield db_pool
    await _wipe(db_pool)


async def _mk(pool, *, title: str, days: int) -> dict:
    today = await pool.fetchval("SELECT CURRENT_DATE")
    return await svc.create_expiring_item(
        pool,
        {
            "kind": "passport",
            "title": f"{PREFIX}{title}",
            "expires_on": today + timedelta(days=days),
            "lead_days": [30, 7, 1],
        },
    )


async def _settle(env, predicate, *, tries: int = 200) -> None:
    """Wait, in REAL time, for the abandoned child to reach `predicate`."""
    # Sync context manager (it only flips a flag), even on the async env.
    with env.auto_time_skipping_disabled():
        for _ in range(tries):
            if predicate():
                return
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_due_item_raises_one_card_and_a_rerun_raises_none(pool):
    """The correctness core: same item, two runs, exactly one card and one set
    of ledger rows — and the ledger, not the flow, is what says so."""
    item = await _mk(pool, title="passport", days=5)
    acts = ExpiringItemsActivities(db_pool=pool)
    tq = f"tq-{uuid4().hex[:8]}"

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=tq,
            workflows=[ExpiryRadarFlow, InteractionFlow],
            activities=[
                acts.claim_due_alerts,
                acts.record_expiry_cards,
                _stub_insert,
                _stub_send_card,
                _stub_resolve,
                _stub_timeout,
            ],
        ),
    ):
        first = await env.client.execute_workflow(
            ExpiryRadarFlow.run,
            ExpiryRadarConfig(agent_id=AGENT),
            id=f"expiry-radar-1-{uuid4().hex[:8]}",
            task_queue=tq,
        )
        assert first == {"claimed": 1, "cards": 1}
        await _settle(env, lambda: len(_cards) == 1)
        assert len(_cards) == 1
        assert _cards[0]["kind"] == "ack"
        assert _cards[0]["agent_id"] == AGENT
        assert "expires in <b>5 days</b>" in _cards[0]["prompt"]

        second = await env.client.execute_workflow(
            ExpiryRadarFlow.run,
            ExpiryRadarConfig(agent_id=AGENT),
            id=f"expiry-radar-2-{uuid4().hex[:8]}",
            task_queue=tq,
        )
        # `claimed == 0` is the load-bearing assertion: the second run found
        # nothing left to claim, i.e. the DB ledger suppressed it. A card count
        # alone would be satisfied by the child-workflow-id collision too.
        assert second == {"claimed": 0, "cards": 0}
        await _settle(env, lambda: len(_cards) > 1, tries=10)
        assert len(_cards) == 1

    rows = await pool.fetch(
        "SELECT threshold_days FROM life.expiring_item_alerts "
        "WHERE item_id = $1 ORDER BY threshold_days",
        item["id"],
    )
    assert [r["threshold_days"] for r in rows] == [7, 30]
    # Cards bypass safe_send_message, so the flow books them against the budget
    # itself — one sent card, no failures.
    sent = await pool.fetch(
        "SELECT log_event, sent FROM notification_log WHERE agent_id = $1", AGENT
    )
    assert [(r["log_event"], r["sent"]) for r in sent] == [("expiry_card", True)]


@pytest.mark.asyncio
async def test_item_outside_every_lead_raises_nothing(pool):
    await _mk(pool, title="far-off", days=90)
    acts = ExpiringItemsActivities(db_pool=pool)
    tq = f"tq-{uuid4().hex[:8]}"

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=tq,
            workflows=[ExpiryRadarFlow, InteractionFlow],
            activities=[
                acts.claim_due_alerts,
                acts.record_expiry_cards,
                _stub_insert,
                _stub_send_card,
                _stub_resolve,
                _stub_timeout,
            ],
        ),
    ):
        result = await env.client.execute_workflow(
            ExpiryRadarFlow.run,
            ExpiryRadarConfig(agent_id=AGENT),
            id=f"expiry-radar-none-{uuid4().hex[:8]}",
            task_queue=tq,
        )
        assert result == {"claimed": 0, "cards": 0}
        await _settle(env, lambda: bool(_cards), tries=10)
        assert _cards == []

    # Nothing sent means nothing booked against the notification budget.
    assert await pool.fetchval(
        "SELECT count(*) FROM notification_log WHERE agent_id = $1", AGENT
    ) == 0


@pytest.mark.asyncio
async def test_max_cards_caps_one_run_and_the_surplus_fires_on_the_next(pool):
    """Three overdue items, cap of two: two cards now, the third tomorrow —
    never four, and never a silently dropped item."""
    await _mk(pool, title="a", days=0)
    await _mk(pool, title="b", days=1)
    await _mk(pool, title="c", days=2)
    acts = ExpiringItemsActivities(db_pool=pool)
    tq = f"tq-{uuid4().hex[:8]}"

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=tq,
            workflows=[ExpiryRadarFlow, InteractionFlow],
            activities=[
                acts.claim_due_alerts,
                acts.record_expiry_cards,
                _stub_insert,
                _stub_send_card,
                _stub_resolve,
                _stub_timeout,
            ],
        ),
    ):
        first = await env.client.execute_workflow(
            ExpiryRadarFlow.run,
            ExpiryRadarConfig(agent_id=AGENT, max_cards=2),
            id=f"expiry-radar-cap1-{uuid4().hex[:8]}",
            task_queue=tq,
        )
        assert first == {"claimed": 2, "cards": 2}

        second = await env.client.execute_workflow(
            ExpiryRadarFlow.run,
            ExpiryRadarConfig(agent_id=AGENT, max_cards=2),
            id=f"expiry-radar-cap2-{uuid4().hex[:8]}",
            task_queue=tq,
        )
        assert second == {"claimed": 1, "cards": 1}
        await _settle(env, lambda: len(_cards) == 3)
        assert len(_cards) == 3


def test_flow_and_activities_are_registered():
    """A @workflow.defn / @activity.defn is invisible to Temporal unless it is
    in worker/__main__.py's explicit lists and schedule_sync's type map."""
    import aegis_worker.__main__ as worker_main
    from aegis_worker.schedule_sync import _ACTIVITY_TYPE_MAP

    assert ExpiryRadarFlow in worker_main.WORKFLOWS

    names = [
        activity._Definition.must_from_callable(a).name for a in worker_main.ACTIVITIES
    ]
    assert "claim_due_alerts" in names
    assert "record_expiry_cards" in names

    assert "ExpiryRadarFlow" in _ACTIVITY_TYPE_MAP
    cls, config = _ACTIVITY_TYPE_MAP["ExpiryRadarFlow"](
        {"agent_id": "sebas", "config": {"lookahead_days": 90, "max_cards": 2}}
    )
    assert cls is ExpiryRadarFlow
    assert config == ExpiryRadarConfig(agent_id="sebas", lookahead_days=90, max_cards=2)

    # Defaults must survive an empty config block (the seed row can omit knobs).
    _cls, default_config = _ACTIVITY_TYPE_MAP["ExpiryRadarFlow"](
        {"agent_id": "sebas", "config": {}}
    )
    assert default_config == ExpiryRadarConfig(agent_id="sebas")


def test_seed_row_matches_the_registered_workflow_type():
    """The seed row is how the schedule actually reaches a deployment — a typo
    in workflow_type is a schedule that never resolves to a flow."""
    from pathlib import Path

    import yaml
    from aegis_worker.schedule_sync import _ACTIVITY_TYPE_MAP

    root = Path(__file__).resolve().parents[3]
    rows = yaml.safe_load((root / "config/seed/activities.yaml").read_text())["activities"]
    row = next(r for r in rows if r["slug"] == "expiry-radar-daily")

    assert row["workflow_type"] in _ACTIVITY_TYPE_MAP
    assert row["active"] is True
    # A slot of its own — asserted BEFORE the literal so it is reachable
    # whatever cron the row carries. A cron shared with another row stacks two
    # flows on the same minute.
    assert [r["slug"] for r in rows if r["schedule_cron"] == row["schedule_cron"]] == [
        "expiry-radar-daily"
    ]
    assert row["schedule_cron"] == "25 7 * * *"
