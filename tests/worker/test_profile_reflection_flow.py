"""A2 — ProfileReflectionFlow: one human-approved persona draft per week.

Like the A7 curiosity tests, the REAL `ProfileActivities` and the REAL
`InteractionFlow` run against a real Postgres, so the assertions are about the
`interactions` and `notification_log` rows production would actually write —
not a recording stub of the spawn call. Only two things are faked: the LLM
(`FakeLLM`) and `send_interaction_card`, which is the one step that leaves the
process (POST /api/deliver/card).

The card child is spawned ABANDONED, so it is still working when the parent
returns — every assertion about it is made while the worker is still up, inside
`env.auto_time_skipping_disabled()` so the test server cannot fast-forward the
card's 7-day archive timer while we look.

The other half of the contract — that the payload the admin panel submits is the
payload `apply_profile_reflection` reads — lives in
`tests/worker/test_profile_reflection_e2e.py`.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
import pytest_asyncio
from aegis_worker.activities.interactions import InteractionActivities
from aegis_worker.activities.profile import ProfileActivities
from aegis_worker.flows.interaction import InteractionFlow
from aegis_worker.flows.profile_reflection import ProfileReflectionConfig, ProfileReflectionFlow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

AGENT = "zza2f-profile"
PURPOSE = "profile_reflection"

CURRENT_DOC = "Owner is based in Pune.\nPrefers concise answers.\n" + ("x" * 300)
PROPOSED_DOC = CURRENT_DOC + "\nRuns a homelab swarm called meem."


# --------------------------------------------------------------------- fixtures


async def _wipe(conn):
    await conn.execute("DELETE FROM agent_profile_revisions WHERE agent_id = $1", AGENT)
    await conn.execute("DELETE FROM agent_personalities WHERE agent_id = $1", AGENT)
    await conn.execute("DELETE FROM agent_memory WHERE agent_id = $1", AGENT)
    await conn.execute("DELETE FROM interactions WHERE agent_id = $1", AGENT)
    await conn.execute("DELETE FROM notification_log WHERE agent_id = $1", AGENT)
    await conn.execute("DELETE FROM llm_calls WHERE agent_id = $1", AGENT)
    # `test_a_quiet_week_sends_nothing` asserts the flow SKIPS for want of
    # evidence, and two of the five evidence sources are global — no agent_id
    # column on `finance.*`, and every `calendar_events_%` settings row counts.
    # Any other test file's leftovers are this agent's evidence, which is how a
    # stray fixture turned this into `assert 'carded' == 'skipped'` on pristine
    # `main` (issue #220). Cleared here rather than chased at each source.
    # Children before parents: both FK to `recurring_charge`.
    await conn.execute("DELETE FROM settings WHERE key LIKE 'calendar_events_%'")
    await conn.execute("DELETE FROM finance.renewal_alert")
    await conn.execute("DELETE FROM finance.receipt_email")
    await conn.execute("DELETE FROM finance.recurring_charge")


@pytest_asyncio.fixture(loop_scope="function")
async def clean_db(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, role, system_prompt_path, active) "
            "VALUES ($1, $1, 'assistant', 'personalities/x', TRUE) "
            "ON CONFLICT (id) DO NOTHING",
            AGENT,
        )
        await _wipe(conn)
    yield db_pool
    async with db_pool.acquire() as conn:
        await _wipe(conn)
        await conn.execute("DELETE FROM agents WHERE id = $1", AGENT)


class FakeLLM:
    def __init__(
        self,
        *,
        response: str = "",
        raises: BaseException | None = None,
        by_purpose: dict[str, str] | None = None,
    ):
        self._response = response
        self._raises = raises
        # A5: the flow now makes TWO calls with different `purpose` values
        # (profile_generalization, then profile_reflection). Keyed replies let a
        # test answer each one without stubbing the activity.
        self._by_purpose = by_purpose or {}
        self.calls: list[dict] = []

    async def think(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return {
            "response": self._by_purpose.get(kwargs.get("purpose", ""), self._response),
            "model": "fake-balanced",
            "prompt_tokens": 5,
            "completion_tokens": 7,
        }

    def prompt_for(self, purpose: str) -> str:
        return next(
            (str(c.get("prompt") or "") for c in self.calls if c.get("purpose") == purpose), ""
        )


def _llm_reply(doc: str = PROPOSED_DOC) -> str:
    return json.dumps(
        {
            "proposed_doc": doc,
            "rationale": "the owner mentioned a homelab twice this week",
            "changed_lines": ["+ Runs a homelab swarm called meem."],
        }
    )


async def _seed_doc(pool, content: str = CURRENT_DOC):
    await pool.execute(
        "INSERT INTO agent_personalities (agent_id, kind, content) VALUES ($1,'user',$2) "
        "ON CONFLICT (agent_id, kind) DO UPDATE SET content = EXCLUDED.content",
        AGENT,
        content,
    )
    from aegis.services.personalities import invalidate

    invalidate(AGENT)


async def _seed_evidence(pool):
    await pool.execute(
        "INSERT INTO agent_memory (agent_id, content, importance, source) "
        "VALUES ($1, 'Owner runs a homelab swarm called meem', 0.9, 'correction')",
        AGENT,
    )


# A5: three memories saying the same thing — the promotable pattern.
A5_TRIPLE = [
    "Owner refuses meetings before 11am",
    "Owner moved the standup because it was before 11am",
    "Owner asked never to be booked in the morning",
]
A5_CLAIM = "The owner does not take meetings in the morning."


async def _seed_triple(pool) -> list[int]:
    ids = []
    for content in A5_TRIPLE:
        ids.append(
            await pool.fetchval(
                "INSERT INTO agent_memory (agent_id, content, importance, source) "
                "VALUES ($1, $2, 0.7, 'correction') RETURNING id",
                AGENT,
                content,
            )
        )
    return sorted(ids)


def _generalization_reply(ids: list[int], confidence: float = 0.9) -> str:
    return json.dumps(
        [{"claim": A5_CLAIM, "supporting_memory_ids": ids, "confidence": confidence}]
    )


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


@asynccontextmanager
async def _flow_worker(client, pool, *, llm=None, cards=None, extra=None, drop=()):
    prof = ProfileActivities(db_pool=pool, llm_client=llm)
    inter = InteractionActivities(pool)
    task_queue = f"tq-{uuid4().hex[:8]}"
    activities = [
        prof.check_profile_budget,
        prof.read_profile_context,
        prof.gather_profile_evidence,
        prof.propose_generalizations,
        prof.propose_profile_patch,
        prof.record_profile_card,
        prof.apply_profile_reflection,
        inter.insert_interaction,
        inter.resolve_interaction,
        inter.apply_interaction_timeout,
        inter.update_interaction_delivery_ref,
        _stub_card(cards if cards is not None else []),
    ]
    if drop:
        activities = [
            a for a in activities if activity._Definition.must_from_callable(a).name not in drop
        ]
    activities += list(extra or [])
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[ProfileReflectionFlow, InteractionFlow],
        activities=activities,
    ):

        async def run(config=None):
            return await client.execute_workflow(
                ProfileReflectionFlow.run,
                config or ProfileReflectionConfig(agent_id=AGENT),
                id=f"profile-test-{uuid4().hex[:8]}",
                task_queue=task_queue,
            )

        yield run


async def _cards_count(pool) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM interactions WHERE origin = $1 AND agent_id = $2", PURPOSE, AGENT
    )


async def _budget_rows(pool) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM notification_log WHERE log_event = $1 AND agent_id = $2",
        PURPOSE,
        AGENT,
    )


# ------------------------------------------------------------------------ tests


@pytest.mark.asyncio
async def test_flow_cards_the_draft_and_charges_the_budget(clean_db):
    """Happy path: a week with evidence becomes exactly one `draft_review` card
    carrying the proposed document, and charges the notification budget."""
    await _seed_doc(clean_db)
    await _seed_evidence(clean_db)
    cards: list = []
    llm = FakeLLM(response=_llm_reply())

    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            async with _flow_worker(env.client, clean_db, llm=llm, cards=cards) as run:
                result = await run(
                    ProfileReflectionConfig(
                        agent_id=AGENT, aegis_ui_url="https://aegis.example.com"
                    )
                )
                row = await _wait_for(
                    lambda: clean_db.fetchrow(
                        "SELECT agent_id, kind, origin, prompt, options, metadata, status, "
                        "timeout_policy FROM interactions WHERE origin = $1 AND agent_id = $2",
                        PURPOSE,
                        AGENT,
                    )
                )
                await _wait_for(_async(lambda: len(cards)))

    assert result["status"] == "carded"
    assert result["carded"] == 1
    assert result["evidence_total"] >= 1

    assert row is not None, "no profile_reflection interaction row was created"
    assert row["kind"] == "draft_review"
    assert row["status"] == "pending"
    assert row["timeout_policy"] == "archive"
    # The document the human is being asked to approve travels in metadata —
    # the card prompt is only a summary, and the admin panel's editor is
    # initialised from `metadata.proposed_doc`.
    assert row["metadata"]["proposed_doc"] == PROPOSED_DOC
    assert row["metadata"]["agent_id"] == AGENT
    assert row["metadata"]["kind"] == "user"
    assert row["metadata"]["revision_of"]
    assert "homelab" in row["prompt"]

    # Nothing has been written to the persona yet — that is the whole point.
    assert (
        await clean_db.fetchval(
            "SELECT content FROM agent_personalities WHERE agent_id = $1 AND kind = 'user'", AGENT
        )
        == CURRENT_DOC
    )
    assert await clean_db.fetchval(
        "SELECT count(*) FROM agent_profile_revisions WHERE agent_id = $1", AGENT
    ) == 0

    # `aegis_ui_url` is load-bearing: aegis_comms/cards.py renders NO button for
    # a `draft_review` without it, so the Slack card would be a dead end.
    assert len(cards) == 1
    assert cards[0]["kind"] == "draft_review"
    assert cards[0]["options"] == {"aegis_ui_url": "https://aegis.example.com"}

    logged = await clean_db.fetch(
        "SELECT agent_id, sent FROM notification_log WHERE log_event = $1 AND agent_id = $2",
        PURPOSE,
        AGENT,
    )
    assert len(logged) == 1
    assert logged[0]["sent"] is True


@pytest.mark.asyncio
async def test_a_quiet_week_sends_nothing(clean_db):
    """Acceptance: an empty evidence bundle → `skipped`, no card, no charge,
    and the LLM is never asked."""
    await _seed_doc(clean_db)
    llm = FakeLLM(response=_llm_reply())

    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            async with _flow_worker(env.client, clean_db, llm=llm) as run:
                result = await run()
                await asyncio.sleep(0.5)
                spawned = await _cards_count(clean_db)

    assert result["status"] == "skipped"
    assert result["reason"] == "no_evidence"
    assert result["carded"] == 0
    assert spawned == 0
    assert await _budget_rows(clean_db) == 0
    assert llm.calls == []


@pytest.mark.asyncio
async def test_an_llm_failure_is_a_quiet_week_not_a_failed_run(clean_db):
    await _seed_doc(clean_db)
    await _seed_evidence(clean_db)
    llm = FakeLLM(raises=RuntimeError("litellm 503"))

    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            async with _flow_worker(env.client, clean_db, llm=llm) as run:
                result = await run()
                await asyncio.sleep(0.5)
                spawned = await _cards_count(clean_db)

    assert result["status"] == "skipped"
    assert result["reason"] == "llm_failed"
    assert spawned == 0
    assert await _budget_rows(clean_db) == 0


@pytest.mark.asyncio
async def test_a_proposal_identical_to_the_current_doc_is_not_carded(clean_db):
    """"Here is your document, unchanged" is the fastest way to train the owner
    to ignore the card."""
    await _seed_doc(clean_db)
    await _seed_evidence(clean_db)
    llm = FakeLLM(response=_llm_reply(doc=CURRENT_DOC))

    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            async with _flow_worker(env.client, clean_db, llm=llm) as run:
                result = await run()
                await asyncio.sleep(0.5)
                spawned = await _cards_count(clean_db)

    assert result["status"] == "skipped"
    assert result["reason"] == "no_change"
    assert spawned == 0
    assert await _budget_rows(clean_db) == 0


@pytest.mark.asyncio
async def test_a_proposal_activity_blow_up_degrades_to_skipped(clean_db):
    """The activity swallows LLM faults itself, so the flow's own guard only
    fires when the ACTIVITY fails — a Temporal timeout, a dead worker, a DB
    error on the way out. Proven by failing the activity, not the model:
    without this, the flow's `except` around propose_profile_patch is never
    executed by any test (the LLM-failure test above passes with it deleted)."""
    await _seed_doc(clean_db)
    await _seed_evidence(clean_db)

    @activity.defn(name="propose_profile_patch")
    async def exploding(agent_id, evidence, current_doc, generalizations=None):
        raise RuntimeError("worker lost the task")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            async with _flow_worker(
                env.client,
                clean_db,
                llm=FakeLLM(response=_llm_reply()),
                drop=("propose_profile_patch",),
                extra=[exploding],
            ) as run:
                result = await run()
                await asyncio.sleep(0.5)
                spawned = await _cards_count(clean_db)

    assert result["status"] == "skipped"
    assert result["reason"] == "llm_failed"
    assert spawned == 0
    assert await _budget_rows(clean_db) == 0


@pytest.mark.asyncio
async def test_an_evidence_blow_up_degrades_to_skipped(clean_db):
    await _seed_doc(clean_db)
    await _seed_evidence(clean_db)

    @activity.defn(name="gather_profile_evidence")
    async def exploding(agent_id, lookback_days):
        raise RuntimeError("chat_history is on fire")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            async with _flow_worker(
                env.client,
                clean_db,
                llm=FakeLLM(response=_llm_reply()),
                drop=("gather_profile_evidence",),
                extra=[exploding],
            ) as run:
                result = await run()
                await asyncio.sleep(0.5)
                spawned = await _cards_count(clean_db)

    assert result["status"] == "skipped"
    assert result["reason"] == "evidence_failed"
    assert spawned == 0
    assert await _budget_rows(clean_db) == 0


@pytest.mark.asyncio
async def test_second_run_the_same_day_is_budget_blocked(clean_db):
    """Acceptance: two runs in one day → the second spawns nothing and says
    `budget`; notification_log still holds exactly one sent row."""
    await _seed_doc(clean_db)
    await _seed_evidence(clean_db)
    cards: list = []
    llm = FakeLLM(response=_llm_reply())

    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            async with _flow_worker(env.client, clean_db, llm=llm, cards=cards) as run:
                first = await run()
                await _wait_for(_async(lambda: len(cards)))
                second = await run()
                await asyncio.sleep(0.5)
                spawned = await _cards_count(clean_db)

    assert first["status"] == "carded"
    assert second["status"] == "budget"
    assert second["carded"] == 0
    assert spawned == 1, "the second run spawned another card"
    assert len(cards) == 1, "the second run dispatched another card"
    sent = await clean_db.fetch(
        "SELECT sent FROM notification_log WHERE log_event = $1 AND agent_id = $2", PURPOSE, AGENT
    )
    assert [r["sent"] for r in sent] == [True]


# ------------------------------------------------------------------------- A5


async def _card_metadata(pool):
    return await pool.fetchval(
        "SELECT metadata FROM interactions WHERE origin = $1 AND agent_id = $2", PURPOSE, AGENT
    )


@pytest.mark.asyncio
async def test_generalizations_reach_the_prompt_and_the_card(clean_db):
    """A5 end to end through the flow: three agreeing memories become a claim,
    the claim is weighted into the drafting prompt, and BOTH the claim and the
    ids it rests on land in the card the human answers.

    `promoted_memory_ids` is the ledger `_promoted_memory_ids` reads back, so if
    it never reaches the metadata the promotion can never become idempotent.
    """
    await _seed_doc(clean_db)
    ids = await _seed_triple(clean_db)
    cards: list = []
    llm = FakeLLM(
        by_purpose={
            "profile_generalization": _generalization_reply(ids),
            "profile_reflection": _llm_reply(),
        }
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            async with _flow_worker(env.client, clean_db, llm=llm, cards=cards) as run:
                result = await run()
                await _wait_for(_async(lambda: len(cards)))
                metadata = await _wait_for(lambda: _card_metadata(clean_db))

    assert result["status"] == "carded"
    assert result["generalizations"] == 1
    assert result["promoted_memory_ids"] == 3

    metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
    assert metadata["promoted_memory_ids"] == ids
    assert [g["claim"] for g in metadata["generalizations"]] == [A5_CLAIM]
    assert metadata["generalizations"][0]["supporting_memory_ids"] == ids

    # The drafting call was actually told about the claim…
    assert A5_CLAIM in llm.prompt_for("profile_reflection")
    # …and the human is told too, on the card itself — an inference about them
    # buried inside a long document is an inference nobody reads.
    assert A5_CLAIM in cards[0]["prompt"]


@pytest.mark.asyncio
async def test_a_low_confidence_claim_never_reaches_the_card(clean_db):
    """Off-week safety: below the confidence bar the claim is logged, and the
    weekly draft still goes out — just without it."""
    await _seed_doc(clean_db)
    ids = await _seed_triple(clean_db)
    cards: list = []
    llm = FakeLLM(
        by_purpose={
            "profile_generalization": _generalization_reply(ids, confidence=0.1),
            "profile_reflection": _llm_reply(),
        }
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            async with _flow_worker(env.client, clean_db, llm=llm, cards=cards) as run:
                result = await run()
                await _wait_for(_async(lambda: len(cards)))
                metadata = await _wait_for(lambda: _card_metadata(clean_db))

    assert result["status"] == "carded"
    assert result["generalizations"] == 0
    metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
    assert metadata["generalizations"] == []
    assert metadata["promoted_memory_ids"] == []
    assert A5_CLAIM not in cards[0]["prompt"]
    assert "(none)" in llm.prompt_for("profile_reflection")


@pytest.mark.asyncio
async def test_a_generalization_activity_blow_up_still_cards(clean_db):
    """The activity swallows its own faults, so the flow's `except` around
    `propose_generalizations` only fires when the ACTIVITY does. Proven by
    failing the activity — and the point of the guard is that the weekly draft
    survives it: replace `gen = {}` with `raise` and this test fails."""
    await _seed_doc(clean_db)
    await _seed_triple(clean_db)
    cards: list = []

    @activity.defn(name="propose_generalizations")
    async def exploding(agent_id):
        raise RuntimeError("worker lost the task")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            async with _flow_worker(
                env.client,
                clean_db,
                llm=FakeLLM(response=_llm_reply()),
                cards=cards,
                drop=("propose_generalizations",),
                extra=[exploding],
            ) as run:
                result = await run()
                await _wait_for(_async(lambda: len(cards)))
                metadata = await _wait_for(lambda: _card_metadata(clean_db))

    assert result["status"] == "carded", "a broken generalisation pass cost the weekly draft"
    assert result["generalizations"] == 0
    metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
    assert metadata["promoted_memory_ids"] == []
    assert await _budget_rows(clean_db) == 1


# ------------------------------------------------------------------ registration


def test_flow_is_registered_on_the_worker():
    from types import SimpleNamespace

    import aegis_worker.__main__ as worker_main
    from aegis_worker.registry import workflows_for

    assert ProfileReflectionFlow in worker_main.WORKFLOWS
    prod = SimpleNamespace(homelab_enabled=True, money_hygiene_enabled=True)
    assert ProfileReflectionFlow in workflows_for(prod)


def test_profile_activities_are_served_by_the_worker():
    """Registration is derived from the @activity.defn methods of the instances
    main() builds, so what matters is that these carry the decorator under
    exactly these names — a rename silently drops them from the served set, and
    `post_resolve_activity` is resolved by NAME at runtime."""
    from aegis_worker.registry import expected_activity_names

    served = expected_activity_names()
    for name in (
        "gather_profile_evidence",
        "propose_generalizations",
        "propose_profile_patch",
        "check_profile_budget",
        "record_profile_card",
        "apply_profile_reflection",
        "read_profile_context",
        "apply_profile_patch",
    ):
        assert name in served, f"{name} is not an activity the worker serves"


def test_flow_in_schedule_map():
    from aegis_worker.schedule_sync import _ACTIVITY_TYPE_MAP

    assert "ProfileReflectionFlow" in _ACTIVITY_TYPE_MAP
    cls, config = _ACTIVITY_TYPE_MAP["ProfileReflectionFlow"](
        {
            "agent_id": "sebas",
            "config": {"lookback_days": 14, "max_per_day": 2},
            "_settings": {"aegis_ui_url": "https://aegis.example.com"},
        }
    )
    assert cls is ProfileReflectionFlow
    assert config.agent_id == "sebas"
    assert config.lookback_days == 14
    assert config.max_per_day == 2
    assert config.aegis_ui_url == "https://aegis.example.com"


def test_profile_reflection_seed_row_exists():
    from pathlib import Path

    import yaml

    repo = Path(__file__).resolve().parents[2]
    rows = yaml.safe_load((repo / "config" / "seed" / "activities.yaml").read_text())["activities"]
    row = next((r for r in rows if r["slug"] == "profile-reflection-weekly"), None)
    assert row is not None, "profile-reflection-weekly missing from config/seed/activities.yaml"
    assert row["workflow_type"] == "ProfileReflectionFlow"
    assert row["schedule_cron"] == "23 2 * * 0"
    assert row["active"] is True

    # Must not land on a slot another activity already owns…
    others = [r["schedule_cron"] for r in rows if r["slug"] != "profile-reflection-weekly"]
    assert row["schedule_cron"] not in others
    # …nor on a minute one of the sub-hourly steppers already fires on. This is
    # the reason the seed deviates from the spec's "0 2 * * 0": minute :00 is
    # the busiest minute in the file.
    minute = int(row["schedule_cron"].split()[0])
    for cron in others:
        field = cron.split()[0]
        if field.startswith("*/"):
            assert minute % int(field[2:]) != 0, f"minute {minute} collides with {cron}"
        elif field == "*":
            raise AssertionError(f"every-minute cron in the seed: {cron}")
