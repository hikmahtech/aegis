"""A7 — CuriosityCardFlow: one budget-aware question per day.

The flow tests wire the REAL CuriosityActivities and the REAL InteractionFlow
(plus the real InteractionActivities) against a real Postgres, so what they
assert on is the `interactions` row and the `notification_log` row production
would actually write — not a recording stub of the spawn call.
`send_interaction_card` is the only stub: it is the one step that leaves the
process (POST /api/deliver/card). Its captured payload is asserted as a card
*spec*, never rendered here — `aegis_comms` is not installed in the worker CI
job, and the Block Kit rendering of that spec belongs to
`tests/comms/test_cards_slack.py`.

The card child is spawned ABANDONED, so it is still working when the parent
workflow returns — every assertion about it has to be made while the worker is
still up, and always inside `env.auto_time_skipping_disabled()` so the test
server cannot fast-forward the card's 2-day archive timer while we look.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
import pytest_asyncio
from aegis_worker.activities.curiosity import CuriosityActivities
from aegis_worker.activities.interactions import InteractionActivities
from aegis_worker.flows.curiosity import CuriosityCardFlow, CuriosityConfig
from aegis_worker.flows.interaction import InteractionFlow
from temporalio import activity
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

AGENT = "sebas"
# Who resolve_agents hands the card to. Deliberately NOT `AGENT`, so an
# assertion on the card's agent proves the routing hop really happened.
FINANCE_AGENT = "curio-finance-test"


# --------------------------------------------------------------------- fixtures


async def _wipe(conn):
    await conn.execute("DELETE FROM notification_log")
    await conn.execute("DELETE FROM interactions")
    # Children before parents: finance.renewal_alert.charge_id and
    # finance.receipt_email.charge_id both reference recurring_charge with no
    # ON DELETE CASCADE, and the money tests leave alert rows behind. Without
    # this the unqualified charge delete raises ForeignKeyViolationError and
    # errors every test in this file — but only when those files happen to
    # share an xdist worker with it.
    await conn.execute("DELETE FROM finance.renewal_alert")
    await conn.execute("DELETE FROM finance.receipt_email WHERE charge_id IS NOT NULL")
    await conn.execute("DELETE FROM finance.recurring_charge")
    await conn.execute("DELETE FROM agent_memory WHERE agent_id = ANY($1)", [AGENT, FINANCE_AGENT])
    await conn.execute("DELETE FROM agent_personalities WHERE agent_id = $1", AGENT)
    await conn.execute("DELETE FROM chat_history WHERE agent_id = $1", AGENT)
    await conn.execute("DELETE FROM knowledge_chunks")
    await conn.execute("DELETE FROM knowledge_content")
    # Children before parents (todoist_notes.item_id -> todoist_tasks(id) has no
    # ON DELETE CASCADE, and other files leave notes behind); see test_daylog.py.
    await conn.execute("DELETE FROM todoist_notes")
    await conn.execute("DELETE FROM todoist_tasks")
    await conn.execute("DELETE FROM todoist_projects")


@pytest_asyncio.fixture(loop_scope="function")
async def clean_db(db_pool):
    async with db_pool.acquire() as conn:
        for agent in (AGENT, FINANCE_AGENT):
            await conn.execute(
                "INSERT INTO agents (id, name, role, system_prompt_path, active) "
                "VALUES ($1, $1, 'assistant', 'personalities/x', TRUE) "
                "ON CONFLICT (id) DO NOTHING",
                agent,
            )
        await _wipe(conn)
    yield db_pool
    async with db_pool.acquire() as conn:
        await _wipe(conn)
        # FINANCE_AGENT is this file's own row; AGENT is a real seeded agent
        # other files rely on, so only the private one is dropped.
        await conn.execute("DELETE FROM agents WHERE id = $1", FINANCE_AGENT)


# ---------------------------------------------------------------------- seeding


async def _add_charge(pool, vendor: str, monthly: float = 900.0):
    await pool.execute(
        "INSERT INTO finance.recurring_charge "
        "(account, sender_label, vendor_name, category, amount_cents, currency, "
        " monthly_home_equivalent, cadence, status) "
        "VALUES ($1, $1, $2, 'software', 90000, 'INR', $3, 'monthly', 'active')",
        f"acct-{vendor}",
        vendor,
        monthly,
    )


async def _add_calendar_attendee(pool, email: str, events: int = 3):
    """Exactly what CalendarIngestFlow files — the `Attendees:` line the A6
    detector parses out of the ingested chunk text."""
    for _ in range(events):
        cid = f"cal-{uuid4().hex[:8]}"
        await pool.execute(
            "INSERT INTO knowledge_content (content_id, url, title, source_type) "
            "VALUES ($1, $2, 'Sync', 'calendar')",
            cid,
            f"calendar://{cid}",
        )
        await pool.execute(
            "INSERT INTO knowledge_chunks (content_id, chunk_index, chunk_text) "
            "VALUES ($1, 0, $2)",
            cid,
            f"Sync\nAttendees: {email}",
        )


async def _add_pending_curiosity_card(pool, novelty_key: str = "charge:leftover"):
    await pool.execute(
        "INSERT INTO interactions (flow_run_id, agent_id, kind, origin, prompt, "
        "status, metadata) VALUES ($1, $2, 'input', 'curiosity', 'q?', 'pending', $3)",
        f"run-{uuid4()}",
        AGENT,
        {"novelty_key": novelty_key},
    )


# ------------------------------------------------------------------- flow runner


async def _wait_for(coro_factory, timeout: float = 15.0):
    """Poll an async predicate until truthy — the ABANDONED child races us."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = await coro_factory()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            return value
        await asyncio.sleep(0.05)


def _async(fn):
    """Adapt a plain predicate for `_wait_for`."""

    async def _wrapped():
        return fn()

    return _wrapped


def _stub_card(log: list):
    @activity.defn(name="send_interaction_card")
    async def send_interaction_card(
        interaction_id, agent_id, kind, prompt, options, allow_hint=False
    ):
        log.append(
            {
                "interaction_id": interaction_id,
                "agent_id": agent_id,
                "kind": kind,
                "prompt": prompt,
                "options": options,
            }
        )
        return {"ok": True, "delivery_ref": {"adapter": "web"}}

    return send_interaction_card


def _stub_resolve(tags_log: list, mapping: dict):
    @activity.defn(name="resolve_agents")
    async def resolve_agents(tags):
        tags_log.append(list(tags))
        return {t: mapping.get(t) for t in tags}

    return resolve_agents


@asynccontextmanager
async def _flow_worker(client, pool, *, cur_kwargs=None, resolve=None, cards=None):
    """A live worker running the flow + the REAL InteractionFlow.

    Yields a `run(config)` callable. Everything the test asserts on has to
    happen INSIDE this block: the card child is ABANDONED, so it is still
    working when the parent returns and would be cut off mid-flight by the
    worker shutting down.
    """
    cur = CuriosityActivities(db_pool=pool, llm_client=None, **(cur_kwargs or {}))
    inter = InteractionActivities(pool)
    task_queue = f"tq-{uuid4().hex[:8]}"
    activities = [
        cur.check_curiosity_budget,
        cur.find_curiosity_gaps,
        cur.record_curiosity_card,
        cur.apply_curiosity_answer,
        inter.insert_interaction,
        inter.resolve_interaction,
        inter.apply_interaction_timeout,
        inter.update_interaction_delivery_ref,
        _stub_card(cards if cards is not None else []),
        resolve or _stub_resolve([], {}),
    ]
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[CuriosityCardFlow, InteractionFlow],
        activities=activities,
    ):

        async def run(config=None):
            return await client.execute_workflow(
                CuriosityCardFlow.run,
                config or CuriosityConfig(agent_id=AGENT),
                id=f"curiosity-test-{uuid4().hex[:8]}",
                task_queue=task_queue,
            )

        yield run


async def _cards_count(pool) -> int:
    return await pool.fetchval("SELECT count(*) FROM interactions WHERE origin = 'curiosity'")


async def _budget_rows(pool) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM notification_log WHERE log_event = 'curiosity_card'"
    )


# ------------------------------------------------------------------------ tests


@pytest.mark.asyncio
async def test_flow_cards_the_top_gap_and_charges_the_budget(clean_db):
    """Happy path: one unexplained charge becomes one `input` card, routed to
    the finance-tagged agent, and charged to the notification budget."""
    await _add_charge(clean_db, "Zephyrly")
    tags: list = []
    cards: list = []

    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            async with _flow_worker(
                env.client,
                clean_db,
                resolve=_stub_resolve(tags, {"finance": FINANCE_AGENT}),
                cards=cards,
            ) as run:
                result = await run(
                    CuriosityConfig(agent_id=AGENT, aegis_ui_url="https://aegis.example.com")
                )
                row = await _wait_for(
                    lambda: clean_db.fetchrow(
                        "SELECT agent_id, kind, origin, prompt, options, metadata, status "
                        "FROM interactions WHERE origin = 'curiosity'"
                    )
                )
                # The card dispatch is a second activity after insert_interaction.
                await _wait_for(_async(lambda: len(cards)))

    assert result["status"] == "sent"
    assert result["gap_type"] == "recurring_charge"
    assert result["subject"] == "Zephyrly"
    # A money gap is asked by the finance-tagged agent, not the flow's own agent.
    assert tags == [["finance"]]
    assert result["agent_id"] == FINANCE_AGENT

    assert row is not None, "no curiosity interaction row was created"
    assert row["agent_id"] == FINANCE_AGENT
    assert row["kind"] == "input"
    assert row["status"] == "pending"
    assert "Zephyrly" in row["prompt"]
    assert row["metadata"]["novelty_key"] == "charge:zephyrly"
    assert row["metadata"]["subject"] == "Zephyrly"

    # The spec handed to the comms service is an ANSWERABLE `input` card.
    # `aegis_ui_url` is load-bearing, not decoration: aegis_comms/cards.py
    # renders NO button at all for `input` unless `options.aegis_ui_url` is
    # present, so a card without it is unanswerable from Slack. The rendering
    # half of that contract is owned by
    # tests/comms/test_cards_slack.py::test_input_url_button (the comms CI job);
    # this side asserts the worker emits exactly the key that test consumes.
    # Deliberately no `aegis_comms` import here — the worker CI job installs
    # core+worker only, and aegis-comms is independent by design.
    assert len(cards) == 1
    assert cards[0]["kind"] == "input"
    assert cards[0]["options"] == {"aegis_ui_url": "https://aegis.example.com"}
    assert cards[0]["interaction_id"]

    # Charged to the same daily budget as a proactive FYI.
    logged = await clean_db.fetch(
        "SELECT agent_id, sent FROM notification_log WHERE log_event = 'curiosity_card'"
    )
    assert len(logged) == 1
    assert logged[0]["sent"] is True
    assert logged[0]["agent_id"] == FINANCE_AGENT


@pytest.mark.asyncio
async def test_second_run_same_day_is_budget_blocked(clean_db):
    """Acceptance: two runs in one day → the second spawns nothing and says
    `budget`; notification_log still holds exactly one sent curiosity_card."""
    await _add_charge(clean_db, "Zephyrly")
    await _add_charge(clean_db, "Brambleworks", monthly=100.0)
    cards: list = []

    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            async with _flow_worker(
                env.client,
                clean_db,
                resolve=_stub_resolve([], {"finance": FINANCE_AGENT}),
                cards=cards,
            ) as run:
                first = await run()
                await _wait_for(_async(lambda: len(cards)))
                second = await run()
                # Give a (wrongly) spawned second card time to land before counting.
                await asyncio.sleep(0.5)
                spawned = await _cards_count(clean_db)

    assert first["status"] == "sent"
    assert second["status"] == "budget"
    assert second["carded"] == 0
    assert spawned == 1, "the second run spawned another card"
    assert len(cards) == 1, "the second run dispatched another card"
    sent = await clean_db.fetch(
        "SELECT sent FROM notification_log WHERE log_event = 'curiosity_card'"
    )
    assert [r["sent"] for r in sent] == [True]


@pytest.mark.asyncio
async def test_pending_card_blocks_a_new_question(clean_db):
    """An unanswered curiosity card suppresses the next one — with no
    notification_log row in play, so it is the pending check doing the work."""
    await _add_charge(clean_db, "Zephyrly")
    await _add_pending_curiosity_card(clean_db)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            async with _flow_worker(env.client, clean_db) as run:
                result = await run()
                await asyncio.sleep(0.5)
                spawned = await _cards_count(clean_db)
                budget_rows = await _budget_rows(clean_db)

    assert result["status"] == "pending"
    assert spawned == 1, "the flow carded on top of an unanswered question"
    assert budget_rows == 0


@pytest.mark.asyncio
async def test_shared_notification_budget_blocks_the_card(clean_db):
    """The flow honours the SHARED daily budget too (`should_send`), not just
    its own per-day cap — a day already full of proactive FYIs stays quiet."""
    await _add_charge(clean_db, "Zephyrly")
    await clean_db.execute(
        "INSERT INTO notification_log (agent_id, log_event, sent) VALUES ($1, 'drift', TRUE)",
        AGENT,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            async with _flow_worker(
                env.client, clean_db, cur_kwargs={"budget_enabled": True, "daily_budget": 1}
            ) as run:
                result = await run()
                await asyncio.sleep(0.5)
                spawned = await _cards_count(clean_db)

    assert result["status"] == "global_budget"
    assert spawned == 0


@pytest.mark.asyncio
async def test_calendar_lane_is_refused_while_owner_emails_is_unset(clean_db):
    """The guard: with `owner_emails` empty (the current deployment state) a
    calendar attendee cannot be shown to be someone other than the owner, so
    the lane is refused outright rather than risk asking the owner who they
    are. Configuring `owner_emails` re-opens it for a non-owner address."""
    await _add_calendar_attendee(clean_db, "zara@example.com", events=3)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            async with _flow_worker(env.client, clean_db) as run:
                refused = await run()
                await asyncio.sleep(0.5)
                spawned_after_refusal = await _cards_count(clean_db)
                budget_after_refusal = await _budget_rows(clean_db)

            # Same data, `owner_emails` configured to the OWNER's own address:
            # the attendee is now provably not the owner, so it gets asked.
            async with _flow_worker(
                env.client,
                clean_db,
                cur_kwargs={"owner_emails": frozenset({"me@example.com"})},
                resolve=_stub_resolve([], {"gtd": AGENT}),
            ) as run:
                allowed = await run()
                row = await _wait_for(
                    lambda: clean_db.fetchrow(
                        "SELECT metadata FROM interactions WHERE origin = 'curiosity'"
                    )
                )

    assert refused["status"] == "no_gaps"
    assert spawned_after_refusal == 0, "asked about a calendar attendee with owner_emails unset"
    assert budget_after_refusal == 0
    assert allowed["status"] == "sent"
    assert allowed["gap_type"] == "calendar_attendee"
    assert row is not None and row["metadata"]["novelty_key"] == "attendee:zara@example.com"


@pytest.mark.asyncio
async def test_gap_detection_failure_degrades_to_skipped(clean_db):
    """An LLM/detector blow-up is a quiet day, not a failed workflow run."""
    await _add_charge(clean_db, "Zephyrly")

    @activity.defn(name="find_curiosity_gaps")
    async def exploding(agent_id, limit):
        raise RuntimeError("llm exploded")

    cur = CuriosityActivities(db_pool=clean_db, llm_client=None)
    inter = InteractionActivities(clean_db)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            task_queue = f"tq-{uuid4().hex[:8]}"
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[CuriosityCardFlow, InteractionFlow],
                activities=[
                    cur.check_curiosity_budget,
                    cur.record_curiosity_card,
                    exploding,
                    inter.insert_interaction,
                    _stub_card([]),
                    _stub_resolve([], {}),
                ],
            ):
                result = await env.client.execute_workflow(
                    CuriosityCardFlow.run,
                    CuriosityConfig(agent_id=AGENT),
                    id=f"curiosity-boom-{uuid4().hex[:8]}",
                    task_queue=task_queue,
                )
                await asyncio.sleep(0.5)
                spawned = await _cards_count(clean_db)

    assert result["status"] == "skipped"
    assert result["carded"] == 0
    assert spawned == 0
    assert await _budget_rows(clean_db) == 0


# ------------------------------------------------------- apply_curiosity_answer


async def _resolved_interaction(pool, prompt: str = "Who is Zara?") -> str:
    row = await pool.fetchrow(
        "INSERT INTO interactions (flow_run_id, agent_id, kind, origin, prompt, status) "
        "VALUES ($1, $2, 'input', 'curiosity', $3, 'resolved') RETURNING id",
        f"run-{uuid4()}",
        AGENT,
        prompt,
    )
    return str(row["id"])


@pytest.mark.asyncio
async def test_apply_curiosity_answer_banks_one_memory(clean_db):
    iid = await _resolved_interaction(clean_db)
    acts = CuriosityActivities(db_pool=clean_db)
    out = await ActivityEnvironment().run(
        acts.apply_curiosity_answer,
        iid,
        {"value": "  My co-founder at Hikmah.  "},
        {
            "subject": "zara@example.com",
            "question": "Who is Zara?",
            "gap_type": "calendar_attendee",
        },
    )

    assert out["recorded"] is True
    rows = await clean_db.fetch(
        "SELECT content, importance, source, agent_id FROM agent_memory WHERE source = 'curiosity'"
    )
    assert len(rows) == 1
    assert rows[0]["agent_id"] == AGENT
    assert rows[0]["source"] == "curiosity"
    assert rows[0]["importance"] == pytest.approx(0.8, abs=1e-6)
    # The answer itself has to survive into the memory, not just the question.
    assert "My co-founder at Hikmah." in rows[0]["content"]
    assert "Who is Zara?" in rows[0]["content"]


@pytest.mark.asyncio
async def test_apply_curiosity_answer_ignores_an_empty_answer(clean_db):
    iid = await _resolved_interaction(clean_db)
    acts = CuriosityActivities(db_pool=clean_db)
    for response in ({"value": "   "}, {}, None):
        out = await ActivityEnvironment().run(
            acts.apply_curiosity_answer, iid, response, {"subject": "x"}
        )
        assert out == {"recorded": False, "reason": "empty"}
    assert (
        await clean_db.fetchval("SELECT count(*) FROM agent_memory WHERE source = 'curiosity'") == 0
    )


# ------------------------------------------------------------------ registration


def test_flow_is_registered_in_both_workflow_lists():
    import ast
    import inspect

    import aegis_worker.__main__ as worker_main

    assert CuriosityCardFlow in worker_main.WORKFLOWS

    # main() builds its own list; read it via AST so a mention in a comment or
    # a docstring cannot satisfy this.
    tree = ast.parse(inspect.getsource(worker_main))
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "workflows"
            and isinstance(node.value, ast.List)
        ):
            names |= {e.id for e in node.value.elts if isinstance(e, ast.Name)}
    assert "CuriosityCardFlow" in names, "CuriosityCardFlow missing from main()'s workflows list"


def test_curiosity_activities_are_in_the_runtime_activities_list():
    import ast
    import inspect

    import aegis_worker.__main__ as worker_main

    tree = ast.parse(inspect.getsource(worker_main))
    attrs: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "activities"
            and isinstance(node.value, ast.List)
        ):
            attrs |= {e.attr for e in node.value.elts if isinstance(e, ast.Attribute)}

    for name in (
        "find_curiosity_gaps",
        "check_curiosity_budget",
        "record_curiosity_card",
        "apply_curiosity_answer",
    ):
        assert name in attrs, f"{name} missing from main()'s activities list"


def test_flow_in_schedule_map():
    from aegis_worker.schedule_sync import _ACTIVITY_TYPE_MAP

    assert "CuriosityCardFlow" in _ACTIVITY_TYPE_MAP
    cls, config = _ACTIVITY_TYPE_MAP["CuriosityCardFlow"](
        {
            "agent_id": "sebas",
            "config": {"max_per_day": 2},
            "_settings": {"aegis_ui_url": "https://aegis.example.com"},
        }
    )
    assert cls is CuriosityCardFlow
    assert config.agent_id == "sebas"
    assert config.max_per_day == 2
    # Without this the Slack card has no button at all (cards.py renders a URL
    # button for `input` only when options.aegis_ui_url is present).
    assert config.aegis_ui_url == "https://aegis.example.com"


def test_curiosity_seed_row_exists():
    from pathlib import Path

    import yaml

    repo = Path(__file__).resolve().parents[2]
    rows = yaml.safe_load((repo / "config" / "seed" / "activities.yaml").read_text())["activities"]
    row = next((r for r in rows if r["slug"] == "curiosity-daily"), None)
    assert row is not None, "curiosity-daily missing from config/seed/activities.yaml"
    assert row["workflow_type"] == "CuriosityCardFlow"
    assert row["schedule_cron"] == "30 9 * * *"
    assert row["active"] is True
    # Must not land on a slot another activity already owns.
    others = [r["schedule_cron"] for r in rows if r["slug"] != "curiosity-daily"]
    assert row["schedule_cron"] not in others
