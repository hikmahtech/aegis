"""A6 — CuriosityActivities.find_curiosity_gaps against a real Postgres.

Covers the acceptance criteria: a charge with no memory yields a candidate and
a memory naming the vendor removes it; a novelty_key already on a non-archived
interaction is excluded; zero detectors firing returns []; an absent or failing
LLM still yields the candidate with deterministic text.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from aegis_worker.activities.curiosity import CuriosityActivities
from temporalio.testing import ActivityEnvironment

AGENT = "sebas"


@pytest_asyncio.fixture(loop_scope="function")
async def clean_db(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, role, system_prompt_path, active) "
            "VALUES ($1, 'Sebas', 'assistant', 'personalities/sebas', TRUE) "
            "ON CONFLICT (id) DO NOTHING",
            AGENT,
        )
        await _wipe(conn)
    yield db_pool
    async with db_pool.acquire() as conn:
        await _wipe(conn)


async def _wipe(conn):
    # Children before parents: finance.renewal_alert / finance.receipt_email
    # reference recurring_charge with no ON DELETE CASCADE, and the money tests
    # leave alert rows behind — without this the unqualified delete below raises
    # ForeignKeyViolationError and errors every test in this file, but only when
    # those files land on the same xdist worker.
    await conn.execute("DELETE FROM finance.renewal_alert")
    await conn.execute("DELETE FROM finance.receipt_email WHERE charge_id IS NOT NULL")
    await conn.execute("DELETE FROM finance.recurring_charge")
    await conn.execute("DELETE FROM agent_memory WHERE agent_id = $1", AGENT)
    await conn.execute("DELETE FROM agent_personalities WHERE agent_id = $1", AGENT)
    await conn.execute("DELETE FROM interactions")
    await conn.execute("DELETE FROM knowledge_chunks")
    await conn.execute("DELETE FROM knowledge_content")
    await conn.execute("DELETE FROM chat_history WHERE agent_id = $1", AGENT)
    # Children before parents (same reason as tests/worker/test_daylog.py):
    # todoist_notes -> todoist_tasks -> todoist_projects, none cascading, and
    # the clarify tests leave notes behind for whichever xdist worker gets them.
    await conn.execute("DELETE FROM todoist_notes")
    await conn.execute("DELETE FROM todoist_tasks")
    await conn.execute("DELETE FROM todoist_projects")


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


async def _add_interaction(pool, novelty_key: str, status: str = "pending"):
    await pool.execute(
        "INSERT INTO interactions (flow_run_id, agent_id, kind, origin, prompt, "
        "status, metadata) VALUES ($1, $2, 'input', 'curiosity', 'q?', $3, $4)",
        f"run-{uuid4()}",
        AGENT,
        status,
        {"novelty_key": novelty_key},
    )


async def _run(pool, **kw):
    acts = CuriosityActivities(db_pool=pool, **kw)
    return await ActivityEnvironment().run(acts.find_curiosity_gaps, AGENT, 5)


# --------------------------------------------------------------- charge detector


async def test_active_charge_with_no_memory_yields_candidate(clean_db):
    await _add_charge(clean_db, "Framer")

    out = await _run(clean_db)

    assert len(out) == 1, out
    assert out[0]["gap_type"] == "recurring_charge"
    assert out[0]["subject"] == "Framer"
    assert out[0]["novelty_key"] == "charge:framer"
    assert "Framer" in out[0]["question"]


async def test_memory_naming_the_vendor_removes_the_candidate(clean_db):
    await _add_charge(clean_db, "Framer")
    await clean_db.execute(
        "INSERT INTO agent_memory (agent_id, content, importance, source) "
        "VALUES ($1, 'Framer is where the landing pages live.', 0.8, 'curiosity')",
        AGENT,
    )

    assert await _run(clean_db) == []


async def test_profile_naming_the_vendor_also_removes_the_candidate(clean_db):
    await _add_charge(clean_db, "Framer")
    await clean_db.execute(
        "INSERT INTO agent_personalities (agent_id, kind, content) "
        "VALUES ($1, 'user', 'He builds sites on framer.')",
        AGENT,
    )

    assert await _run(clean_db) == []


# ------------------------------------------------------------- novelty-key gate


async def test_novelty_key_on_pending_interaction_is_excluded(clean_db):
    await _add_charge(clean_db, "Framer")
    await _add_interaction(clean_db, "charge:framer", status="pending")

    assert await _run(clean_db) == []


async def test_novelty_key_on_resolved_interaction_is_excluded(clean_db):
    await _add_charge(clean_db, "Framer")
    await _add_interaction(clean_db, "charge:framer", status="resolved")

    assert await _run(clean_db) == []


async def test_archived_interaction_does_not_suppress(clean_db):
    """An unanswered (timed-out) card is not an answered question."""
    await _add_charge(clean_db, "Framer")
    await _add_interaction(clean_db, "charge:framer", status="archived")

    out = await _run(clean_db)
    assert [c["novelty_key"] for c in out] == ["charge:framer"]


# ------------------------------------------------------------------ empty / rank


async def test_no_gaps_returns_empty_list_not_filler(clean_db):
    assert await _run(clean_db) == []


async def test_limit_is_respected_and_ranked_by_cost(clean_db):
    await _add_charge(clean_db, "Cheap", monthly=100.0)
    await _add_charge(clean_db, "Pricey", monthly=5000.0)
    await _add_charge(clean_db, "Middling", monthly=800.0)

    acts = CuriosityActivities(db_pool=clean_db)
    out = await ActivityEnvironment().run(acts.find_curiosity_gaps, AGENT, 2)

    assert [c["subject"] for c in out] == ["Pricey", "Middling"]


# -------------------------------------------------------------- other detectors


async def _add_calendar_event(pool, content_id: str, attendees: str):
    await pool.execute(
        "INSERT INTO knowledge_content (content_id, url, title, source_type) "
        "VALUES ($1, $2, 'Sync', 'calendar')",
        content_id,
        f"calendar://{content_id}",
    )
    await pool.execute(
        "INSERT INTO knowledge_chunks (content_id, chunk_index, chunk_text) "
        "VALUES ($1, 0, $2)",
        content_id,
        f"Sync\nStart: 2026-07-01\nAttendees: {attendees}",
    )


async def test_recurring_calendar_attendee_is_a_gap(clean_db):
    for i in range(3):
        await _add_calendar_event(clean_db, f"ev-{i}", "nadia@example.com")

    out = await _run(clean_db)

    assert [c["novelty_key"] for c in out] == ["attendee:nadia@example.com"]
    assert out[0]["evidence"] == {"events": 3}


async def test_attendee_below_threshold_is_not_a_gap(clean_db):
    for i in range(2):
        await _add_calendar_event(clean_db, f"ev-{i}", "nadia@example.com")

    assert await _run(clean_db) == []


async def test_attendee_mentioned_in_chat_history_is_not_a_gap(clean_db):
    for i in range(3):
        await _add_calendar_event(clean_db, f"ev-{i}", "nadia@example.com")
    await clean_db.execute(
        "INSERT INTO chat_history (thread_id, agent_id, role, content) "
        "VALUES ('t1', $1, 'user', 'ping nadia@example.com about the deck')",
        AGENT,
    )

    assert await _run(clean_db) == []


# ------------------------------------------------------- owner-email exclusion


async def test_owner_email_is_never_a_curiosity_gap(clean_db):
    """Google lists the calendar owner in `attendees` — don't ask who they are."""
    for i in range(4):
        await _add_calendar_event(
            clean_db, f"ev-{i}", "arshad@hikmah.com, nadia@example.com"
        )

    out = await _run(clean_db, owner_emails=frozenset({"arshad@hikmah.com"}))

    assert [c["novelty_key"] for c in out] == ["attendee:nadia@example.com"]


async def test_owner_email_match_is_case_and_whitespace_insensitive(clean_db):
    """However the operator typed it into the admin UI, it must still match.

    Event says `Arshad@Hikmah.COM`; the config value is padded and capitalised
    differently. Both sides are normalized before comparison.
    """
    for i in range(4):
        await _add_calendar_event(
            clean_db, f"ev-{i}", "Arshad@Hikmah.COM, nadia@example.com"
        )

    out = await _run(clean_db, owner_emails=frozenset({"  ARSHAD@hikmah.Com  "}))

    assert [c["novelty_key"] for c in out] == ["attendee:nadia@example.com"]


async def test_unset_owner_emails_excludes_nobody(clean_db):
    """Unconfigured => previous behaviour, no exclusion and no exception."""
    for i in range(3):
        await _add_calendar_event(clean_db, f"ev-{i}", "nadia@example.com")

    out = await _run(clean_db)  # owner_emails defaults to frozenset()

    assert [c["novelty_key"] for c in out] == ["attendee:nadia@example.com"]


def test_owner_emails_are_wired_from_settings_into_the_worker():
    """The dataclass field is useless unless main() actually fills it.

    Must read Settings.owner_emails — the DB-backed integration config — so a
    value set in the admin UI reaches the detector without a redeploy.
    """
    import inspect

    import aegis_worker.__main__ as m

    src = inspect.getsource(m.main)
    assert "owner_emails=frozenset(" in src
    assert 'getattr(settings, "owner_emails", "")' in src


def test_owner_emails_is_a_db_backed_integration_config_key():
    """Env-only would need a redeploy to change — it must be in the registry."""
    from aegis.config import Settings
    from aegis.services.integrations_config import CONFIG_REGISTRY

    spec = next((c for c in CONFIG_REGISTRY if c.key == "owner_emails"), None)
    assert spec is not None, "owner_emails is not in CONFIG_REGISTRY (not admin-settable)"
    assert not spec.secret and not spec.boolean
    # apply_config_overrides only overlays keys that exist on Settings.
    assert Settings.model_fields["owner_emails"].default == ""


async def test_busy_todoist_project_with_no_profile_context_is_a_gap(clean_db):
    await clean_db.execute(
        "INSERT INTO todoist_projects (id, name, is_archived) VALUES ('p1', 'Sabaki', FALSE)"
    )
    for i in range(5):
        await clean_db.execute(
            "INSERT INTO todoist_tasks (id, project_id, content) VALUES ($1, 'p1', $2)",
            f"t{i}",
            f"task {i}",
        )

    out = await _run(clean_db)

    assert [c["novelty_key"] for c in out] == ["project:sabaki"]
    assert out[0]["evidence"] == {"tasks": 5}


async def test_finished_project_is_not_a_gap(clean_db):
    """A project whose tasks are all done doesn't 'carry' anything."""
    await clean_db.execute(
        "INSERT INTO todoist_projects (id, name, is_archived) VALUES ('p1', 'Sabaki', FALSE)"
    )
    for i in range(8):
        await clean_db.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, is_completed) "
            "VALUES ($1, 'p1', $2, TRUE)",
            f"t{i}",
            f"done {i}",
        )

    assert await _run(clean_db) == []


async def test_project_task_count_excludes_completed_tasks(clean_db):
    """The evidence number must be OPEN tasks, not lifetime task volume."""
    await clean_db.execute(
        "INSERT INTO todoist_projects (id, name, is_archived) VALUES ('p1', 'Sabaki', FALSE)"
    )
    for i in range(5):
        await clean_db.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, is_completed) "
            "VALUES ($1, 'p1', $2, FALSE)",
            f"open-{i}",
            f"task {i}",
        )
    for i in range(20):
        await clean_db.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, is_completed) "
            "VALUES ($1, 'p1', $2, TRUE)",
            f"done-{i}",
            f"done {i}",
        )

    out = await _run(clean_db)

    assert [c["novelty_key"] for c in out] == ["project:sabaki"]
    assert out[0]["evidence"] == {"tasks": 5}
    assert "5 of your tasks" in out[0]["question"]


async def test_broken_detector_does_not_kill_the_run(clean_db, monkeypatch):
    """One detector raising costs its own candidates, never the others'."""
    await _add_charge(clean_db, "Framer")

    async def boom(self, agent_id, known):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(CuriosityActivities, "_detect_calendar_attendee", boom)

    out = await _run(clean_db)
    assert [c["subject"] for c in out] == ["Framer"]


# ------------------------------------------------------------------------- LLM


class _FakeLLM:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls: list[dict] = []

    async def think(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return {
            "response": self.response,
            "model": "fake-model",
            "prompt_tokens": 11,
            "completion_tokens": 7,
        }


async def test_llm_absent_gives_deterministic_question(clean_db):
    await _add_charge(clean_db, "Framer")

    out = await _run(clean_db)

    assert len(out) == 1
    assert out[0]["question"].startswith("You have an active monthly charge from Framer")


async def test_llm_failure_degrades_to_template(clean_db):
    await _add_charge(clean_db, "Framer")
    llm = _FakeLLM(exc=RuntimeError("proxy down"))

    out = await _run(clean_db, llm_client=llm)

    assert len(out) == 1
    assert out[0]["question"].startswith("You have an active monthly charge from Framer")
    assert llm.calls, "LLM should have been attempted"


async def test_llm_rephrases_and_logs_the_call(clean_db):
    await _add_charge(clean_db, "Framer")
    llm = _FakeLLM(response='[{"index": 0, "question": "What do you use Framer for?"}]')

    out = await _run(clean_db, llm_client=llm)

    assert out[0]["question"] == "What do you use Framer for?"
    # Unlogged LLM call sites are a known open issue — this one must land in
    # llm_calls with a purpose, and think() must be given db_pool for its own
    # failure rows.
    assert llm.calls[0]["purpose"] == "curiosity_phrasing"
    assert llm.calls[0]["db_pool"] is clean_db
    assert llm.calls[0]["agent_id"] == AGENT
    row = await clean_db.fetchrow(
        "SELECT model, purpose, agent_id, input_tokens, output_tokens FROM llm_calls "
        "WHERE purpose = 'curiosity_phrasing' ORDER BY created_at DESC LIMIT 1"
    )
    assert row is not None, "no llm_calls row recorded"
    assert row["model"] == "fake-model"
    assert row["agent_id"] == AGENT
    assert row["input_tokens"] == 11
    assert row["output_tokens"] == 7
    await clean_db.execute("DELETE FROM llm_calls WHERE purpose = 'curiosity_phrasing'")


async def test_llm_unparseable_response_keeps_template(clean_db):
    """parse_llm_json returns None on prose — the guards must absorb that."""
    await _add_charge(clean_db, "Framer")
    llm = _FakeLLM(response="not json at all")

    out = await _run(clean_db, llm_client=llm)

    assert out[0]["question"].startswith("You have an active monthly charge from Framer")
    await clean_db.execute("DELETE FROM llm_calls WHERE purpose = 'curiosity_phrasing'")


async def test_llm_blank_question_keeps_template(clean_db):
    """A well-formed response with an empty question must not blank the card."""
    await _add_charge(clean_db, "Framer")
    llm = _FakeLLM(response='[{"index": 0, "question": "   "}]')

    out = await _run(clean_db, llm_client=llm)

    assert out[0]["question"].startswith("You have an active monthly charge from Framer")
    await clean_db.execute("DELETE FROM llm_calls WHERE purpose = 'curiosity_phrasing'")


async def test_all_candidates_suppressed_makes_no_llm_call(clean_db):
    """Detectors fired but the novelty gate ate everything — don't pay for an
    LLM call on an empty list."""
    await _add_charge(clean_db, "Framer")
    await _add_interaction(clean_db, "charge:framer")
    llm = _FakeLLM(response="[]")

    assert await _run(clean_db, llm_client=llm) == []
    assert llm.calls == []


async def test_no_gaps_at_all_makes_no_llm_call(clean_db):
    llm = _FakeLLM(response="[]")

    assert await _run(clean_db, llm_client=llm) == []
    assert llm.calls == []


# ------------------------------------------------------------------ registration


def test_find_curiosity_gaps_is_an_activity_def():
    method = CuriosityActivities.find_curiosity_gaps
    assert hasattr(method, "__temporal_activity_definition")


def test_activity_is_registered_on_the_worker():
    """The wiring that actually makes the activity callable from a flow.

    Since D6 the per-activity registration line is gone — main() constructs
    `curiosity_act` and hands the instance to registry.collect_activities,
    which serves all of its @activity.defn methods. Assert both halves: the
    instance is still constructed, and the activity is in the served set.
    """
    import inspect

    import aegis_worker.__main__ as m
    from aegis_worker.registry import expected_activity_names

    assert "find_curiosity_gaps" in expected_activity_names()
    assert "CuriosityActivities(" in inspect.getsource(m.main)


@pytest.mark.parametrize("kw", ["db_pool", "llm_client"])
def test_constructor_takes_pool_and_llm(kw):
    import inspect

    assert kw in inspect.signature(CuriosityActivities).parameters
