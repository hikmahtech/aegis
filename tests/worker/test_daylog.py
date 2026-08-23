"""A8 — DayLogActivities + DayLogFlow.

Activity tests run against a real Postgres on a fixed historic date, so no
other test's data can leak into a bucket. Flow tests wire the REAL
DayLogActivities and the REAL ContentActivities against a recording
knowledge connector, so the assertions about what gets filed exercise the
production gather → distil → ingest path end to end rather than a stub of it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from aegis_worker.activities.content import ContentActivities
from aegis_worker.activities.daylog import DayLogActivities
from aegis_worker.flows.daylog import DayLogConfig, DayLogFlow
from temporalio import activity
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from tests.llm_stub import StubbedLLMClient

# A date nothing else in the suite writes to.
DAY = "2019-03-14"
DAY_TS = datetime(2019, 3, 14, 10, 30, tzinfo=UTC)


# --------------------------------------------------------------------- seeding


async def _wipe(conn):
    await conn.execute("DELETE FROM knowledge_chunks")
    await conn.execute("DELETE FROM knowledge_content")
    await conn.execute("DELETE FROM interactions")
    await conn.execute("DELETE FROM gtd_clarify_log")
    # Children before parents: todoist_notes.item_id REFERENCES todoist_tasks(id)
    # with no ON DELETE CASCADE, and the clarify tests leave notes behind (they
    # only clean up at setup). Under `--dist loadfile` whether they land on this
    # xdist worker is pure file-assignment luck, so the unqualified tasks delete
    # below MUST clear the notes first or it raises ForeignKeyViolationError and
    # errors every test in this file.
    await conn.execute("DELETE FROM todoist_notes")
    await conn.execute("DELETE FROM todoist_tasks")
    await conn.execute("DELETE FROM workflow_runs")
    await conn.execute("DELETE FROM settings WHERE key = 'daylog_state'")


@pytest_asyncio.fixture(loop_scope="function")
async def clean_db(db_pool):
    async with db_pool.acquire() as conn:
        await _wipe(conn)
    yield db_pool
    async with db_pool.acquire() as conn:
        await _wipe(conn)


async def _add_completed_task(pool, content: str, when: datetime = DAY_TS):
    await pool.execute(
        "INSERT INTO todoist_tasks (id, content, is_completed, completed_at) "
        "VALUES ($1, $2, TRUE, $3)",
        f"task-{uuid4().hex[:8]}",
        content,
        when,
    )


async def _add_calendar_event(pool, title: str, start_iso: str):
    """Exactly what CalendarIngestFlow files: content row + chunk carrying the
    `Start:` line that `calendar_event_to_content` synthesizes."""
    cid = f"cal-{uuid4().hex[:8]}"
    await pool.execute(
        "INSERT INTO knowledge_content (content_id, url, title, source_type) "
        "VALUES ($1, $2, $3, 'calendar')",
        cid,
        f"calendar://{cid}",
        title,
    )
    await pool.execute(
        "INSERT INTO knowledge_chunks (content_id, chunk_index, chunk_text) VALUES ($1, 0, $2)",
        cid,
        f"{title}\nStart: {start_iso}\nEnd: {start_iso}\nAttendees: someone@example.com",
    )


async def _add_resolved_interaction(pool, prompt: str, answer: str, when: datetime = DAY_TS):
    await pool.execute(
        "INSERT INTO interactions "
        "(flow_run_id, agent_id, kind, origin, prompt, status, response, resolved_at) "
        "VALUES ($1, 'raphael', 'choice', 'test', $2, 'resolved', $3, $4)",
        f"run-{uuid4().hex[:8]}",
        prompt,
        {"value": answer},
        when,
    )


async def _add_archived_interaction(pool, prompt: str, when: datetime = DAY_TS):
    """A card nobody answered. The `archive` timeout policy sets
    `status='archived', resolved_at=now()` — same stamp a real answer leaves."""
    await pool.execute(
        "INSERT INTO interactions "
        "(flow_run_id, agent_id, kind, origin, prompt, status, resolved_at) "
        "VALUES ($1, 'raphael', 'choice', 'test', $2, 'archived', $3)",
        f"run-{uuid4().hex[:8]}",
        prompt,
        when,
    )


async def _add_clarify_entry(pool, task_content: str, classification: str):
    task_id = f"task-{uuid4().hex[:8]}"
    await pool.execute(
        "INSERT INTO todoist_tasks (id, content) VALUES ($1, $2)", task_id, task_content
    )
    await pool.execute(
        "INSERT INTO gtd_clarify_log "
        "(todoist_task_id, classification, confidence, llm_model, applied, created_at) "
        "VALUES ($1, $2, 0.9, 'test-model', TRUE, $3)",
        task_id,
        classification,
        DAY_TS,
    )


async def _add_failed_run(pool, workflow_type: str, error: str):
    await pool.execute(
        "INSERT INTO workflow_runs "
        "(run_id, workflow_id, workflow_type, status, started_at, completed_at, error) "
        "VALUES ($1, $2, $3, 'failed', $4, $4, $5)",
        f"r-{uuid4().hex[:8]}",
        f"w-{uuid4().hex[:8]}",
        workflow_type,
        DAY_TS,
        error,
    )


async def _add_email(pool, title: str):
    await pool.execute(
        "INSERT INTO knowledge_content (content_id, url, title, source_type, ingested_at) "
        "VALUES ($1, $2, $3, 'email', $4)",
        f"mail-{uuid4().hex[:8]}",
        f"mail://{uuid4().hex[:8]}",
        title,
        DAY_TS,
    )


# ------------------------------------------------------------------ gathering


@pytest.mark.asyncio
async def test_gather_day_events_buckets_task_calendar_and_interaction(clean_db):
    await _add_completed_task(clean_db, "Ship the pgvector migration")
    await _add_calendar_event(clean_db, "Standup with Zara", f"{DAY}T09:00:00+05:30")
    await _add_resolved_interaction(clean_db, "Renew the domain?", "yes")
    await _add_clarify_entry(clean_db, "Call the plumber", "next")
    await _add_email(clean_db, "Invoice from Hetzner")
    await _add_failed_run(clean_db, "RssIngestFlow", "feed timeout")

    acts = DayLogActivities(db_pool=clean_db)
    out = await ActivityEnvironment().run(acts.gather_day_events, DAY)

    assert [t["content"] for t in out["tasks"]] == ["Ship the pgvector migration"]
    assert [m["title"] for m in out["meetings"]] == ["Standup with Zara"]
    assert len(out["decisions"]) == 1
    assert out["decisions"][0]["prompt"] == "Renew the domain?"
    assert out["decisions"][0]["answer"] == "yes"
    assert [c["content"] for c in out["captures"]] == ["Call the plumber"]
    assert [e["title"] for e in out["email"]] == ["Invoice from Hetzner"]
    assert [f["workflow_type"] for f in out["failures"]] == ["RssIngestFlow"]
    assert out["quiet"] is False
    assert out["counts"] == {
        "meetings": 1,
        "tasks": 1,
        "decisions": 1,
        "captures": 1,
        "email": 1,
        "failures": 1,
    }


@pytest.mark.asyncio
async def test_gather_day_events_excludes_archived_interactions(clean_db):
    """A card that TIMED OUT unanswered is not a decision.

    `handle_interaction_timeout`'s archive policy stamps `resolved_at` exactly
    like a real answer does, so a resolved_at-only window files
    "Decided: <prompt>" for a question nobody ever answered.
    """
    await _add_resolved_interaction(clean_db, "Renew the domain?", "yes")
    await _add_archived_interaction(clean_db, "Approve the invoice?")

    acts = DayLogActivities(db_pool=clean_db)
    out = await ActivityEnvironment().run(acts.gather_day_events, DAY)

    assert [d["prompt"] for d in out["decisions"]] == ["Renew the domain?"]
    assert out["counts"]["decisions"] == 1


@pytest.mark.asyncio
async def test_gather_day_events_excludes_other_days(clean_db):
    await _add_completed_task(clean_db, "Yesterday's task", DAY_TS - timedelta(days=1))
    await _add_completed_task(clean_db, "Today's task", DAY_TS)
    await _add_calendar_event(clean_db, "Tomorrow's meeting", "2019-03-15T09:00:00+05:30")

    acts = DayLogActivities(db_pool=clean_db)
    out = await ActivityEnvironment().run(acts.gather_day_events, DAY)

    assert [t["content"] for t in out["tasks"]] == ["Today's task"]
    assert out["meetings"] == []


class _PoolFailingOn:
    """Delegating pool proxy that raises for queries touching one table."""

    def __init__(self, pool, table: str):
        self._pool = pool
        self._table = table

    async def fetch(self, sql, *args):
        if self._table in sql:
            raise RuntimeError(f"permission denied for relation {self._table}")
        return await self._pool.fetch(sql, *args)

    async def execute(self, sql, *args):
        return await self._pool.execute(sql, *args)


@pytest.mark.asyncio
async def test_gather_day_events_degrades_a_failing_source_to_an_empty_bucket(clean_db):
    await _add_completed_task(clean_db, "Ship the pgvector migration")
    await _add_clarify_entry(clean_db, "Call the plumber", "next")
    await _add_resolved_interaction(clean_db, "Renew the domain?", "yes")

    acts = DayLogActivities(db_pool=_PoolFailingOn(clean_db, "todoist_tasks"))
    out = await ActivityEnvironment().run(acts.gather_day_events, DAY)

    # Both buckets whose query touches the broken table degrade to empty (the
    # rows exist — an unseeded bucket would be empty either way) and nothing
    # else is affected.
    assert out["tasks"] == []
    assert out["captures"] == []  # gtd_clarify_log joins todoist_tasks
    assert [d["prompt"] for d in out["decisions"]] == ["Renew the domain?"]
    assert out["quiet"] is False


@pytest.mark.asyncio
async def test_gather_day_events_marks_an_empty_day_quiet(clean_db):
    acts = DayLogActivities(db_pool=clean_db)
    out = await ActivityEnvironment().run(acts.gather_day_events, DAY)

    assert out["quiet"] is True
    assert out["counts"] == dict.fromkeys(
        ("meetings", "tasks", "decisions", "captures", "email", "failures"), 0
    )


# ------------------------------------------------------------------ distilling


_EVENTS = {
    "date": DAY,
    "meetings": [{"title": "Standup with Zara"}],
    "tasks": [{"content": "Ship the pgvector migration"}],
    "decisions": [{"prompt": "Renew the domain?", "answer": "yes"}],
    "captures": [{"content": "Call the plumber", "classification": "next"}],
    "email": [{"title": "Invoice from Hetzner"}],
    "failures": [{"workflow_type": "RssIngestFlow", "error": "feed timeout"}],
    "counts": {"meetings": 1, "tasks": 1, "decisions": 1, "captures": 1, "email": 1, "failures": 1},
    "quiet": False,
}


@pytest.mark.asyncio
async def test_distil_daylog_without_an_llm_names_the_real_items():
    acts = DayLogActivities(db_pool=None, llm_client=None)
    text = await ActivityEnvironment().run(acts.distil_daylog, _EVENTS, DAY)

    for needle in (
        "Standup with Zara",
        "Ship the pgvector migration",
        "Renew the domain?",
        "Call the plumber",
        "Invoice from Hetzner",
        "RssIngestFlow",
    ):
        assert needle in text, f"deterministic day log lost {needle!r}"


@pytest.mark.asyncio
async def test_distil_daylog_falls_back_when_the_llm_raises():
    class _Boom:
        async def think(self, **kwargs):
            raise RuntimeError("model unreachable")

    acts = DayLogActivities(db_pool=None, llm_client=_Boom())
    text = await ActivityEnvironment().run(acts.distil_daylog, _EVENTS, DAY)

    assert "Ship the pgvector migration" in text


@pytest.mark.asyncio
async def test_distil_daylog_falls_back_when_the_llm_returns_empty():
    class _Empty:
        async def think(self, **kwargs):
            return {"response": "   ", "prompt_tokens": 1, "completion_tokens": 0}

    acts = DayLogActivities(db_pool=None, llm_client=_Empty())
    text = await ActivityEnvironment().run(acts.distil_daylog, _EVENTS, DAY)

    assert "Ship the pgvector migration" in text


@pytest.mark.asyncio
async def test_distil_daylog_logs_the_call_to_llm_calls(clean_db):
    """Unlogged LLM call sites are a known open issue — this one must land.

    Driven through a real `LLMClient` (stubbed HTTP only) because the row is
    written by `LLMClient._record_call`; a fake `think()` would record nothing
    and the assertion would test the fake.
    """
    await clean_db.execute("DELETE FROM llm_calls WHERE purpose = 'daylog_narrative'")
    llm = StubbedLLMClient(
        db_pool=clean_db, content="A calm Thursday.", prompt_tokens=11, completion_tokens=7
    )
    acts = DayLogActivities(db_pool=clean_db, llm_client=llm, model="test-model")
    text = await ActivityEnvironment().run(acts.distil_daylog, _EVENTS, DAY, "raphael")

    assert text == "A calm Thursday."
    rows = await clean_db.fetch(
        "SELECT model, purpose, agent_id, input_tokens, output_tokens FROM llm_calls "
        "WHERE purpose = 'daylog_narrative'"
    )
    # Exactly one — a second row would mean the activity records on top of the
    # choke point and inflates reported spend.
    assert len(rows) == 1, f"expected one daylog llm_calls row, got {len(rows)}"
    assert rows[0]["model"] == "test-model"
    assert rows[0]["agent_id"] == "raphael"
    assert rows[0]["input_tokens"] == 11
    assert rows[0]["output_tokens"] == 7
    await clean_db.execute("DELETE FROM llm_calls WHERE purpose = 'daylog_narrative'")


# ----------------------------------------------------------------------- flow


class _RecordingKS:
    """Stands in for KnowledgeStore: upserts by url, exactly as the real one
    does by content_id, so a re-ingest of the same date can't grow the store."""

    def __init__(self):
        self.entries: dict[str, dict] = {}
        self.calls: list[dict] = []

    async def ingest_content(self, **kwargs):
        self.calls.append(kwargs)
        self.entries[kwargs["url"]] = kwargs
        return {"content_id": f"c-{kwargs['url']}", "status": "ok", "chunks_total": 1}


async def _run_flow(client, ks, day_acts, task_queue: str, extra_activities=None, config=None):
    content_acts = ContentActivities(knowledge_connector=ks, enabled=True)
    activities = [
        day_acts.gather_day_events,
        day_acts.distil_daylog,
        day_acts.commit_daylog_state,
        day_acts.gather_daylogs,
        day_acts.distil_rollup,
        content_acts.ingest_content,
    ]
    if extra_activities:
        # Replace same-named production activities with the supplied stubs.
        names = {activity._Definition.must_from_callable(a).name for a in extra_activities}
        activities = [
            a for a in activities if activity._Definition.must_from_callable(a).name not in names
        ]
        activities += extra_activities
    async with Worker(
        client, task_queue=task_queue, workflows=[DayLogFlow], activities=activities
    ):
        return await client.execute_workflow(
            DayLogFlow.run,
            config or DayLogConfig(agent_id="raphael"),
            id=f"daylog-{uuid4().hex[:8]}",
            task_queue=task_queue,
        )


@pytest.mark.asyncio
async def test_flow_files_one_entry_whose_text_reflects_the_day(clean_db):
    ks = _RecordingKS()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        # Seed against the workflow environment's own clock so the flow's
        # target date and the seeded rows cannot disagree. Auto time-skipping
        # off: the test server otherwise fast-forwards the clock by YEARS
        # between an idle moment and the next run.
        with env.auto_time_skipping_disabled():
            now = await env.get_current_time()
            await _add_completed_task(clean_db, "Ship the pgvector migration", now)
            await _add_calendar_event(
                clean_db, "Standup with Zara", now.strftime("%Y-%m-%dT09:00:00")
            )
            await _add_resolved_interaction(clean_db, "Renew the domain?", "yes", now)

            result = await _run_flow(
                env.client, ks, DayLogActivities(db_pool=clean_db, llm_client=None), "daylog-t1"
            )

    expected_date = now.strftime("%Y-%m-%d")
    assert result["status"] == "ingested"
    assert result["date"] == expected_date
    assert len(ks.calls) == 1
    call = ks.calls[0]
    assert call["source_type"] == "daylog"
    assert call["url"] == f"aegis://daylog/{expected_date}"
    assert call["tags"] == ["daylog"]
    assert call["metadata"]["date"] == expected_date
    assert call["metadata"]["quiet"] is False
    # A call-shape assertion alone passes with distil_daylog returning "" —
    # the day's actual content has to be inside the text that gets filed.
    for needle in ("Ship the pgvector migration", "Standup with Zara", "Renew the domain?"):
        assert needle in call["raw_text"], f"filed day log lost {needle!r}"

    state = await clean_db.fetchval("SELECT value FROM settings WHERE key = 'daylog_state'")
    assert state == {"last_date": expected_date, "url": f"aegis://daylog/{expected_date}"}


@pytest.mark.asyncio
async def test_flow_rerun_for_the_same_date_updates_the_same_entry(clean_db):
    ks = _RecordingKS()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            now = await env.get_current_time()
            await _add_completed_task(clean_db, "Ship the pgvector migration", now)
            acts = DayLogActivities(db_pool=clean_db, llm_client=None)
            first = await _run_flow(env.client, ks, acts, "daylog-t2")
            state_after_first = await clean_db.fetchval(
                "SELECT value FROM settings WHERE key = 'daylog_state'"
            )
            second = await _run_flow(env.client, ks, acts, "daylog-t2")

    assert first["url"] == second["url"]
    assert len(ks.calls) == 2
    assert len(ks.entries) == 1, "a re-run for the same date created a second entry"
    state_after_second = await clean_db.fetchval(
        "SELECT value FROM settings WHERE key = 'daylog_state'"
    )
    assert state_after_second == state_after_first, "daylog_state double-advanced on a re-run"


@pytest.mark.asyncio
async def test_flow_files_a_quiet_day_marked_quiet(clean_db):
    ks = _RecordingKS()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run_flow(
            env.client, ks, DayLogActivities(db_pool=clean_db, llm_client=None), "daylog-t3"
        )

    assert result["status"] == "ingested"
    assert result["quiet"] is True
    assert len(ks.calls) == 1
    assert ks.calls[0]["metadata"]["quiet"] is True
    assert ks.calls[0]["raw_text"].strip()


@pytest.mark.asyncio
async def test_flow_degrades_a_distil_failure_to_a_skipped_run(clean_db):
    """An LLM problem must produce a SKIPPED run, never a FAILED workflow."""

    @activity.defn(name="distil_daylog")
    async def exploding_distil(events: dict, date: str, agent_id: str = "raphael") -> str:
        raise RuntimeError("llm gateway 503")

    ks = _RecordingKS()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run_flow(
            env.client,
            ks,
            DayLogActivities(db_pool=clean_db, llm_client=None),
            "daylog-t4",
            extra_activities=[exploding_distil],
        )

    assert result["status"] == "skipped"
    assert result["reason"] == "distil_failed"
    assert ks.calls == []
    assert await clean_db.fetchval("SELECT value FROM settings WHERE key = 'daylog_state'") is None


@pytest.mark.asyncio
async def test_flow_does_not_advance_the_cursor_when_ingest_is_disabled(clean_db):
    """No knowledge connector ⇒ ingest_content answers "disabled" — the cursor
    must not move, or the day is lost forever."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run_flow(
            env.client, None, DayLogActivities(db_pool=clean_db, llm_client=None), "daylog-t5"
        )

    assert result["status"] == "skipped"
    assert result["reason"] == "ingest_disabled"
    assert await clean_db.fetchval("SELECT value FROM settings WHERE key = 'daylog_state'") is None


# ================================================================ A9 rollups


async def _add_daylog_entry(pool, date: str, chunks: list[str]):
    """Exactly what A8's flow files: a `source_type='daylog'` content row whose
    body lives ONLY in knowledge_chunks (there is no raw-text column)."""
    cid = f"daylog-{date}"
    await pool.execute(
        "INSERT INTO knowledge_content (content_id, url, title, source_type, metadata) "
        "VALUES ($1, $2, $3, 'daylog', $4)",
        cid,
        f"aegis://daylog/{date}",
        f"Day Log {date}",
        {"date": date, "quiet": False, "counts": {}},
    )
    for i, text in enumerate(chunks):
        await pool.execute(
            "INSERT INTO knowledge_chunks (content_id, chunk_index, chunk_text) "
            "VALUES ($1, $2, $3)",
            cid,
            i,
            text,
        )


def _iso_week_dates(now: datetime) -> list[str]:
    monday = now - timedelta(days=now.isocalendar()[2] - 1)
    return [(monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]


# ------------------------------------------------------------- window maths


def test_rollup_window_weekly_anchors_on_the_iso_week():
    from aegis_worker.flows.daylog import rollup_window

    # 2026-08-02 is a Sunday — the scheduled run. ISO week 31 of 2026 runs
    # Mon 2026-07-27 .. Sun 2026-08-02.
    assert rollup_window("weekly", datetime(2026, 8, 2, 20, 20, tzinfo=UTC)) == (
        "2026-07-27",
        "2026-08-02",
        "2026-W31",
    )
    # A manual mid-week run must land on the SAME window/url, not a partial one.
    assert rollup_window("weekly", datetime(2026, 7, 29, 9, 0, tzinfo=UTC)) == (
        "2026-07-27",
        "2026-08-02",
        "2026-W31",
    )
    # ISO year rolls independently of the calendar year: 2027-01-03 is a Sunday
    # belonging to ISO week 53 of 2026.
    assert rollup_window("weekly", datetime(2027, 1, 3, 20, 20, tzinfo=UTC))[2] == "2026-W53"


def test_rollup_window_monthly_fires_only_on_the_last_day_of_the_month():
    from aegis_worker.flows.daylog import rollup_window

    # The 28-31 cron fires up to four times a month; only the real end may run.
    for not_last in (
        datetime(2026, 2, 27, 21, 20, tzinfo=UTC),  # short February
        datetime(2024, 2, 28, 21, 20, tzinfo=UTC),  # LEAP February — 28th is not the end
        datetime(2026, 1, 30, 21, 20, tzinfo=UTC),  # 31-day month
        datetime(2026, 4, 29, 21, 20, tzinfo=UTC),  # 30-day month
    ):
        assert rollup_window("monthly", not_last) is None, f"{not_last} must not roll up"

    assert rollup_window("monthly", datetime(2026, 2, 28, 21, 20, tzinfo=UTC)) == (
        "2026-02-01",
        "2026-02-28",
        "2026-02",
    )
    assert rollup_window("monthly", datetime(2024, 2, 29, 21, 20, tzinfo=UTC)) == (
        "2024-02-01",
        "2024-02-29",
        "2024-02",
    )
    assert rollup_window("monthly", datetime(2026, 1, 31, 21, 20, tzinfo=UTC))[1] == "2026-01-31"
    assert rollup_window("monthly", datetime(2026, 4, 30, 21, 20, tzinfo=UTC))[1] == "2026-04-30"


# -------------------------------------------------------------- gather/distil


@pytest.mark.asyncio
async def test_gather_daylogs_returns_bodies_in_date_order_and_respects_the_window(clean_db):
    for d, body in (
        ("2019-03-11", "Monday: shipped the pgvector migration."),
        ("2019-03-13", "Wednesday: standup with Zara about the outage."),
        ("2019-03-12", "Tuesday: renewed the domain."),
        ("2019-03-18", "Next week: out of window entirely."),
    ):
        await _add_daylog_entry(clean_db, d, [body])
    # A non-daylog document inside the same window must not be swept in.
    await _add_email(clean_db, "Some newsletter")

    acts = DayLogActivities(db_pool=clean_db)
    got = await ActivityEnvironment().run(acts.gather_daylogs, "2019-03-11", "2019-03-17")

    assert [e["date"] for e in got] == ["2019-03-11", "2019-03-12", "2019-03-13"]
    # The BODY is the point — metadata-only would make the rollup contentless.
    assert "pgvector migration" in got[0]["text"]
    assert "renewed the domain" in got[1]["text"]
    assert "standup with Zara" in got[2]["text"]


@pytest.mark.asyncio
async def test_gather_daylogs_stitches_overlapping_chunks_without_repeating(clean_db):
    """KnowledgeStore chunks with a 200-char overlap, so a naive concat repeats
    a paragraph inside every rollup it files."""
    overlap = "the overlap paragraph. "
    await _add_daylog_entry(
        clean_db,
        "2019-03-11",
        [f"Head of the day. {overlap}", f"{overlap}Tail of the day."],
    )

    acts = DayLogActivities(db_pool=clean_db)
    got = await ActivityEnvironment().run(acts.gather_daylogs, "2019-03-11", "2019-03-11")

    assert got[0]["text"] == f"Head of the day. {overlap}Tail of the day."
    assert got[0]["text"].count(overlap) == 1


@pytest.mark.asyncio
async def test_distil_rollup_without_an_llm_concatenates_the_entries():
    entries = [
        {"date": "2019-03-11", "title": "Day Log", "text": "shipped the pgvector migration"},
        {"date": "2019-03-12", "title": "Day Log", "text": "renewed the domain"},
    ]
    acts = DayLogActivities(db_pool=None, llm_client=None)
    text = await ActivityEnvironment().run(acts.distil_rollup, entries, "weekly", "2019-W11")

    assert "2019-W11" in text
    for needle in ("2019-03-11", "shipped the pgvector migration", "renewed the domain"):
        assert needle in text, f"deterministic rollup lost {needle!r}"


@pytest.mark.asyncio
async def test_distil_rollup_llm_failure_degrades_to_the_concatenation():
    class _Boom:
        async def think(self, **kwargs):
            raise RuntimeError("llm gateway 503")

    entries = [
        {"date": "2019-03-11", "title": "Day Log", "text": "shipped the pgvector migration"},
        {"date": "2019-03-12", "title": "Day Log", "text": "renewed the domain"},
    ]
    acts = DayLogActivities(db_pool=None, llm_client=_Boom())
    text = await ActivityEnvironment().run(acts.distil_rollup, entries, "weekly", "2019-W11")

    assert "shipped the pgvector migration" in text


@pytest.mark.asyncio
async def test_both_distil_calls_ask_for_reasoning_room():
    """Both daylog LLM calls must clear the empty-truncation re-roll budget.

    The balanced tier is a reasoning model that bills hidden reasoning_content
    against max_tokens before writing anything, so a budget sized for the prose
    alone (the old 1800/2400, floored to 4096) returns empty or half-written
    text. `clipped` does not raise, so `distil_rollup` files the truncated
    narrative rather than degrading — which is what happened to the 2026-08-16
    weekly rollup. Asking below `_TRUNCATION_RETRY_TOKENS` up front means, at
    best, paying for the same call twice.
    """
    from aegis.llm import _TRUNCATION_RETRY_TOKENS

    seen: list[int] = []

    class _Capture:
        async def think(self, **kwargs):
            seen.append(kwargs["max_tokens"])
            return {"response": "prose"}

    acts = DayLogActivities(db_pool=None, llm_client=_Capture(), model="test-model")
    env = ActivityEnvironment()
    await env.run(acts.distil_daylog, {"date": DAY}, DAY)
    await env.run(
        acts.distil_rollup,
        [{"date": DAY, "text": "a"}, {"date": DAY, "text": "b"}],
        "weekly",
        "2019-W11",
    )

    assert len(seen) == 2, f"expected both distil calls to reach the LLM, got {seen}"
    for budget in seen:
        assert budget >= _TRUNCATION_RETRY_TOKENS, (
            f"daylog distil asks for {budget} tokens, below the "
            f"{_TRUNCATION_RETRY_TOKENS} the truncation re-roll uses — a "
            "reasoning model will clip the prose or return nothing"
        )


@pytest.mark.asyncio
async def test_distil_rollup_records_the_llm_call(clean_db):
    await clean_db.execute("DELETE FROM llm_calls WHERE purpose = 'daylog_rollup'")
    llm = StubbedLLMClient(
        db_pool=clean_db, content="A busy week.", prompt_tokens=21, completion_tokens=9
    )
    acts = DayLogActivities(db_pool=clean_db, llm_client=llm, model="test-model")
    entries = [{"date": "2019-03-11", "text": "a"}, {"date": "2019-03-12", "text": "b"}]
    text = await ActivityEnvironment().run(acts.distil_rollup, entries, "weekly", "2019-W11")

    assert text == "A busy week."
    rows = await clean_db.fetch(
        "SELECT model, purpose, input_tokens, output_tokens FROM llm_calls "
        "WHERE purpose = 'daylog_rollup'"
    )
    assert len(rows) == 1, f"expected one rollup llm_calls row, got {len(rows)}"
    assert rows[0]["input_tokens"] == 21
    assert rows[0]["output_tokens"] == 9
    await clean_db.execute("DELETE FROM llm_calls WHERE purpose = 'daylog_rollup'")


# ---------------------------------------------------------------- rollup flow


@pytest.mark.asyncio
async def test_weekly_rollup_files_one_entry_covering_every_day_of_the_week(clean_db):
    ks = _RecordingKS()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        # Auto time-skipping OFF: the test server otherwise jumps the clock by
        # YEARS between runs, and every assertion here is date-sensitive.
        with env.auto_time_skipping_disabled():
            now = await env.get_current_time()
            dates = _iso_week_dates(now)
            for i, d in enumerate(dates):
                await _add_daylog_entry(clean_db, d, [f"Day {i}: worked on marker-{i}."])
            result = await _run_flow(
                env.client,
                ks,
                DayLogActivities(db_pool=clean_db, llm_client=None),
                "daylog-w1",
                config=DayLogConfig(agent_id="raphael", mode="weekly"),
            )

    iso = now.isocalendar()
    label = f"{iso[0]}-W{iso[1]:02d}"
    assert result["status"] == "ingested"
    assert result["label"] == label
    assert len(ks.calls) == 1
    call = ks.calls[0]
    assert call["source_type"] == "daylog_rollup"
    assert call["url"] == f"aegis://daylog/week/{label}"
    assert call["metadata"]["period"] == "weekly"
    assert call["metadata"]["covers"] == dates, "rollup did not cover all seven days"
    # Not just their existence: the filed text must carry the days' content.
    for i in range(7):
        assert f"marker-{i}" in call["raw_text"], f"rollup lost day {i}'s content"


@pytest.mark.asyncio
async def test_rollup_with_fewer_than_two_entries_writes_nothing(clean_db):
    ks = _RecordingKS()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            now = await env.get_current_time()
            await _add_daylog_entry(clean_db, _iso_week_dates(now)[0], ["Only one day recorded."])
            result = await _run_flow(
                env.client,
                ks,
                DayLogActivities(db_pool=clean_db, llm_client=None),
                "daylog-w2",
                config=DayLogConfig(agent_id="raphael", mode="weekly"),
            )

    assert result["status"] == "insufficient"
    assert result["entries"] == 1
    assert ks.calls == [], "a one-day 'rollup' was filed"


def _first_of_next_month(when: datetime) -> datetime:
    return (when.replace(day=1) + timedelta(days=32)).replace(day=1)


async def _run_monthly_at(clean_db, ks, day_in_month, task_queue: str):
    """Drive the test server's clock to a chosen day-of-month and run the
    monthly rollup there, so neither branch of the month-end guard depends on
    what date the suite happens to run.

    `day_in_month` picks a day of the month AFTER the environment's start
    month (a whole clean month, fully seedable): an int, or "last".
    """
    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            start = await env.get_current_time()
        month = _first_of_next_month(start)
        end_of_month = _first_of_next_month(month) - timedelta(days=1)
        day = end_of_month.day if day_in_month == "last" else day_in_month
        target = month.replace(day=day, hour=21, minute=20, second=0, microsecond=0)
        await env.sleep(target - start)

        with env.auto_time_skipping_disabled():
            now = await env.get_current_time()
            assert now.strftime("%Y-%m-%d") == target.strftime("%Y-%m-%d"), (
                f"clock landed on {now} instead of {target}"
            )
            for i in range(1, end_of_month.day + 1):
                await _add_daylog_entry(
                    clean_db, month.replace(day=i).strftime("%Y-%m-%d"), [f"Day {i}: item-{i}."]
                )
            result = await _run_flow(
                env.client,
                ks,
                DayLogActivities(db_pool=clean_db, llm_client=None),
                task_queue,
                config=DayLogConfig(agent_id="raphael", mode="monthly"),
            )
    return result, month, end_of_month


@pytest.mark.asyncio
async def test_monthly_rollup_skips_a_run_that_is_not_the_month_end(clean_db):
    """The 28-31 cron fires up to four times a month; the flow's guard is the
    only thing that stops three of those runs writing a partial month."""
    ks = _RecordingKS()
    result, _, _ = await _run_monthly_at(clean_db, ks, 28, "daylog-m1")

    assert result["status"] == "skipped"
    assert result["reason"] == "not_period_end"
    assert ks.calls == [], "a 28th-of-the-month run filed a monthly rollup"


@pytest.mark.asyncio
async def test_monthly_rollup_files_on_the_last_day_of_the_month(clean_db):
    ks = _RecordingKS()
    result, month, end_of_month = await _run_monthly_at(clean_db, ks, "last", "daylog-m2")

    label = month.strftime("%Y-%m")
    assert result["status"] == "ingested"
    assert result["label"] == label
    assert len(ks.calls) == 1
    assert ks.calls[0]["source_type"] == "daylog_rollup"
    assert ks.calls[0]["url"] == f"aegis://daylog/month/{label}"
    assert ks.calls[0]["metadata"]["period"] == "monthly"
    assert len(ks.calls[0]["metadata"]["covers"]) == end_of_month.day
    assert f"item-{end_of_month.day}." in ks.calls[0]["raw_text"], "rollup lost the final day"


@pytest.mark.asyncio
async def test_daily_mode_is_unchanged_by_the_rollup_branch(clean_db):
    """A8 regression: an explicit mode='daily' must file exactly what A8 filed."""
    ks = _RecordingKS()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            now = await env.get_current_time()
            await _add_completed_task(clean_db, "Ship the pgvector migration", now)
            result = await _run_flow(
                env.client,
                ks,
                DayLogActivities(db_pool=clean_db, llm_client=None),
                "daylog-d1",
                config=DayLogConfig(agent_id="raphael", mode="daily"),
            )

    expected_date = now.strftime("%Y-%m-%d")
    assert result["status"] == "ingested"
    assert result["date"] == expected_date
    assert len(ks.calls) == 1
    assert ks.calls[0]["source_type"] == "daylog"
    assert ks.calls[0]["url"] == f"aegis://daylog/{expected_date}"
    assert ks.calls[0]["tags"] == ["daylog"]
    assert "Ship the pgvector migration" in ks.calls[0]["raw_text"]


# --------------------------------------------------------------- registration


def test_daylog_flow_is_registered():
    import aegis_worker.__main__ as worker_main

    assert DayLogFlow in worker_main.WORKFLOWS, (
        "DayLogFlow must be registered in worker/__main__.py WORKFLOWS list"
    )


def test_daylog_activities_are_served_by_the_worker():
    """The worker serves every @activity.defn of every instance main() builds
    (registry.collect_activities, boot-checked by check_registration), so what
    still matters here is that these five carry the decorator under exactly
    these names — a rename silently drops them from the served set."""
    from aegis_worker.registry import expected_activity_names

    served = expected_activity_names()
    for name in (
        "gather_day_events",
        "distil_daylog",
        "commit_daylog_state",
        "gather_daylogs",
        "distil_rollup",
    ):
        assert name in served, f"{name} is not an activity the worker serves"


def test_daylog_flow_in_schedule_map():
    from aegis_worker.schedule_sync import _ACTIVITY_TYPE_MAP

    assert "DayLogFlow" in _ACTIVITY_TYPE_MAP
    cls, config = _ACTIVITY_TYPE_MAP["DayLogFlow"](
        {"agent_id": "raphael", "config": {"day_offset": 2}}
    )
    assert cls is DayLogFlow
    assert config.agent_id == "raphael"
    assert config.day_offset == 2
    # Absent mode must stay daily, or an existing daylog-nightly row would
    # silently change behaviour on deploy.
    assert config.mode == "daily"
    _, weekly = _ACTIVITY_TYPE_MAP["DayLogFlow"](
        {"agent_id": "raphael", "config": {"mode": "weekly"}}
    )
    assert weekly.mode == "weekly", "_ACTIVITY_TYPE_MAP does not read config.mode"


def test_daylog_seed_row_exists():
    from pathlib import Path

    import yaml

    repo = Path(__file__).resolve().parents[2]
    rows = yaml.safe_load((repo / "config" / "seed" / "activities.yaml").read_text())["activities"]
    row = next((r for r in rows if r["slug"] == "daylog-nightly"), None)
    assert row is not None, "daylog-nightly missing from config/seed/activities.yaml"
    assert row["workflow_type"] == "DayLogFlow"
    # 19:00 UTC = 00:30 IST — the run's UTC date is the IST day just closed.
    assert row["schedule_cron"] == "0 19 * * *"
    assert row["active"] is True
    # A9 must not have changed the nightly row's mode out from under it.
    assert (row["config"] or {}).get("mode", "daily") == "daily"


def test_daylog_rollup_seed_rows_exist():
    from pathlib import Path

    import yaml

    repo = Path(__file__).resolve().parents[2]
    rows = yaml.safe_load((repo / "config" / "seed" / "activities.yaml").read_text())["activities"]
    by_slug = {r["slug"]: r for r in rows}

    for slug, cron, mode in (
        ("daylog-weekly", "20 20 * * 0", "weekly"),
        ("daylog-monthly", "20 21 28-31 * *", "monthly"),
    ):
        row = by_slug.get(slug)
        assert row is not None, f"{slug} missing from config/seed/activities.yaml"
        assert row["workflow_type"] == "DayLogFlow"
        assert row["schedule_cron"] == cron
        assert row["config"]["mode"] == mode, f"{slug} would run in daily mode"
        assert row["active"] is True

    # The three rows must not share a minute-of-hour slot with each other, and
    # none may sit on the :00 pile-up every */N schedule already lands on.
    minutes = {by_slug[s]["schedule_cron"].split()[0] for s in ("daylog-weekly", "daylog-monthly")}
    assert "0" not in minutes, "rollups scheduled onto the busiest minute in the file"
