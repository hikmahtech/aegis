"""JournalPromptFlow (vault record spec §3): no card unless the day is a gap,
one card per day, and the answer reaches the hook but not the run's result.

The REAL InteractionFlow and InteractionActivities run against the real
Postgres, so the assertions are on the `interactions` row production writes.
The clock, the gap check, the card's delivery and the hook are stubs: the
first two are tested in test_journal_prompt_activities.py against a real
vault. The card child is ABANDONED, so everything about it is asserted while
the worker is up, with time skipping off so its 22-hour timer cannot fire.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
import yaml
from aegis_worker.activities.interactions import InteractionActivities
from aegis_worker.flows.interaction import InteractionFlow
from aegis_worker.flows.journal_prompt import JournalPromptConfig, JournalPromptFlow
from aegis_worker.registry import FLOWS
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

AGENT = "sebas"
DAY = "2026-09-21"  # the day before the stub clock's 2026-09-22
SEED = Path(__file__).resolve().parents[2] / "config" / "seed" / "activities.yaml"


@pytest_asyncio.fixture(loop_scope="function")
async def clean_db(db_pool):
    async def wipe():
        await db_pool.execute("DELETE FROM interactions WHERE origin = 'journal_prompt'")

    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, active) "
        "VALUES ('sebas', 'Sebas', 'assistant', 'personalities/sebas', TRUE) "
        "ON CONFLICT (id) DO NOTHING"
    )
    await wipe()
    yield db_pool
    await wipe()


def _clock():
    @activity.defn(name="daylog_local_day")
    async def daylog_local_day(now_iso: str) -> dict:
        return {"timezone": "UTC", "date": "2026-09-22"}

    return daylog_local_day


def _check(result):
    @activity.defn(name="journal_gap_check")
    async def journal_gap_check(day: str, min_words: int) -> dict:
        if isinstance(result, Exception):
            raise result
        return {"day": day, **result}

    return journal_gap_check


def _card(log: list):
    @activity.defn(name="send_interaction_card")
    async def send_interaction_card(
        interaction_id, agent_id, kind, prompt, options, allow_hint=False
    ):
        log.append({"kind": kind, "prompt": prompt, "options": options})
        return {"ok": True, "delivery_ref": {"adapter": "web"}}

    return send_interaction_card


def _hook(log: list):
    @activity.defn(name="file_journal_answer")
    async def file_journal_answer(interaction_id: str, response: dict, metadata: dict) -> dict:
        log.append({"id": interaction_id, "response": response, "metadata": metadata})
        return {"status": "written", "path": "journal/2026/09. Sep/21 Sep 26.md"}

    return file_journal_answer


@asynccontextmanager
async def _flow_worker(client, pool, *, check, cards=None, answers=None):
    inter = InteractionActivities(pool)
    task_queue = f"tq-{uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[JournalPromptFlow, InteractionFlow],
        activities=[
            _clock(),
            _check(check),
            _card(cards if cards is not None else []),
            _hook(answers if answers is not None else []),
            inter.insert_interaction,
            inter.resolve_interaction,
            inter.apply_interaction_timeout,
            inter.update_interaction_delivery_ref,
        ],
    ):

        async def run(config=None):
            return await client.execute_workflow(
                JournalPromptFlow.run,
                config or JournalPromptConfig(agent_id=AGENT),
                id=f"journal-prompt-test-{uuid4().hex[:8]}",
                task_queue=task_queue,
            )

        yield run


async def _cards(pool) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM interactions WHERE origin = 'journal_prompt'"
    )


async def _wait_for(factory, timeout: float = 15.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = await factory()
        if value or asyncio.get_running_loop().time() > deadline:
            return value
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["not_configured", "disabled", "unreadable", "wrote", "answered", "encrypted"]
)
async def test_only_a_gap_sends_a_card(clean_db, status):
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        _flow_worker(env.client, clean_db, check={"status": status}) as run,
    ):
        result = await run()
    assert result == {"status": "no_card", "reason": status, "day": DAY, "words": None}
    assert await _cards(clean_db) == 0


@pytest.mark.asyncio
async def test_a_check_that_fails_sends_no_card(clean_db):
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        _flow_worker(env.client, clean_db, check=RuntimeError("the pull hung")) as run,
    ):
        result = await run()
    assert result == {"status": "skipped", "reason": "check_failed", "day": DAY}
    assert await _cards(clean_db) == 0


@pytest.mark.asyncio
async def test_a_gap_sends_one_input_card_that_expires_in_22_hours(clean_db):
    cards: list = []
    gap = {"status": "gap", "words": 2, "day_name": "Monday 21 September"}
    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            async with _flow_worker(env.client, clean_db, check=gap, cards=cards) as run:
                result = await run(
                    JournalPromptConfig(agent_id=AGENT, aegis_ui_url="https://aegis.example")
                )
                # The delivery ref is written after the card is sent, so the
                # row having one means `cards` holds the send.
                row = await _wait_for(
                    lambda: clean_db.fetchrow(
                        "SELECT kind, agent_id, prompt, options, metadata, timeout_policy, "
                        "timeout_at - created_at AS ttl FROM interactions "
                        "WHERE origin = 'journal_prompt' AND delivery_ref IS NOT NULL"
                    )
                )
    assert result == {"status": "sent", "day": DAY, "words": 2}
    assert (row["kind"], row["agent_id"], row["timeout_policy"]) == ("input", AGENT, "archive")
    assert row["prompt"].startswith("Nothing in your journal for Monday 21 September.")
    assert row["prompt"].endswith("Ignore this if you already wrote the day on your phone.")
    assert row["options"] == {"label": "Your day", "aegis_ui_url": "https://aegis.example"}
    assert row["metadata"] == {"day": DAY, "agent_id": AGENT}
    assert timedelta(hours=21, minutes=50) < row["ttl"] < timedelta(hours=22, minutes=10)
    assert [c["kind"] for c in cards] == ["input"]


@pytest.mark.asyncio
async def test_one_day_gets_one_card_and_only_the_hook_sees_the_answer(clean_db):
    cards: list = []
    answers: list = []
    gap = {"status": "gap", "words": 0, "day_name": "Monday 21 September"}
    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            async with _flow_worker(
                env.client, clean_db, check=gap, cards=cards, answers=answers
            ) as run:
                assert (await run())["status"] == "sent"
                assert await run() == {"status": "duplicate", "day": DAY}, "still open"
                await _wait_for(lambda: _cards(clean_db))
                child = env.client.get_workflow_handle(f"journal-prompt-{DAY}")
                await child.signal("submit_response", {"value": "Walked to the lake."})
                done = await child.result()
                assert await run() == {"status": "duplicate", "day": DAY}, (
                    "a card that closed is not sent again for the same day"
                )
    assert len(cards) == 1 and await _cards(clean_db) == 1
    assert len(answers) == 1
    assert answers[0]["response"] == {"value": "Walked to the lake."}
    assert answers[0]["metadata"] == {"day": DAY, "agent_id": AGENT}
    assert done["status"] == "resolved" and done["response"] is None


def test_the_seed_row_ships_inactive_at_an_evening_hour():
    """The seed's crons are UTC, with the local time for its example user in
    the comment (the file's own convention); 11:30 UTC is 17:00 at UTC+5:30.
    An operator sets their own on the Flows page."""
    rows = yaml.safe_load(SEED.read_text("utf-8"))["activities"]
    row = next(r for r in rows if r["slug"] == "journal-prompt-daily")
    assert row["workflow_type"] == "JournalPromptFlow"
    assert row["active"] is False
    assert row["schedule_cron"] == "30 11 * * *"
    build = next(s for s in FLOWS if s.flow is JournalPromptFlow).schedule_config
    cfg = build(
        {
            "agent_id": row["agent_id"],
            "config": row["config"],
            "_settings": {"aegis_ui_url": "https://aegis.example"},
        }
    )
    assert (cfg.agent_id, cfg.min_words, cfg.timeout_seconds) == (row["agent_id"], 5, 22 * 3600)
    assert cfg.aegis_ui_url == "https://aegis.example"
    assert cfg.prompt.endswith("Ignore this if you already wrote the day on your phone.")
    custom = build(
        {
            "agent_id": "x",
            "config": {"prompt": "How was {day}?", "label": "Day", "min_words": 3},
            "_settings": {},
        }
    )
    assert (custom.prompt, custom.label, custom.min_words) == ("How was {day}?", "Day", 3)
