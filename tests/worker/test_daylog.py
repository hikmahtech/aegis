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

# A date nothing else in the suite writes to.
DAY = "2019-03-14"
DAY_TS = datetime(2019, 3, 14, 10, 30, tzinfo=UTC)


# --------------------------------------------------------------------- seeding


async def _wipe(conn):
    await conn.execute("DELETE FROM knowledge_chunks")
    await conn.execute("DELETE FROM knowledge_content")
    await conn.execute("DELETE FROM interactions")
    await conn.execute("DELETE FROM gtd_clarify_log")
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
    """Unlogged LLM call sites are a known open issue — this one must land."""
    seen: dict = {}

    class _Ok:
        async def think(self, **kwargs):
            seen.update(kwargs)
            return {
                "response": "A calm Thursday.",
                "model": "test-model",
                "prompt_tokens": 11,
                "completion_tokens": 7,
            }

    await clean_db.execute("DELETE FROM llm_calls WHERE purpose = 'daylog_narrative'")
    acts = DayLogActivities(db_pool=clean_db, llm_client=_Ok(), model="test-model")
    text = await ActivityEnvironment().run(acts.distil_daylog, _EVENTS, DAY, "raphael")

    assert text == "A calm Thursday."
    # think() itself gets the pool + purpose so its OWN failure path records too.
    assert seen["purpose"] == "daylog_narrative"
    assert seen["db_pool"] is clean_db
    row = await clean_db.fetchrow(
        "SELECT model, purpose, agent_id, input_tokens, output_tokens FROM llm_calls "
        "WHERE purpose = 'daylog_narrative'"
    )
    assert row is not None, "successful daylog LLM call was not recorded in llm_calls"
    assert row["model"] == "test-model"
    assert row["agent_id"] == "raphael"
    assert row["input_tokens"] == 11
    assert row["output_tokens"] == 7
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


async def _run_flow(client, ks, day_acts, task_queue: str, extra_activities=None):
    content_acts = ContentActivities(knowledge_connector=ks, enabled=True)
    activities = [
        day_acts.gather_day_events,
        day_acts.distil_daylog,
        day_acts.commit_daylog_state,
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
            DayLogConfig(agent_id="raphael"),
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


# --------------------------------------------------------------- registration


def test_daylog_flow_is_registered():
    import aegis_worker.__main__ as worker_main

    assert DayLogFlow in worker_main.WORKFLOWS, (
        "DayLogFlow must be registered in worker/__main__.py WORKFLOWS list"
    )


def test_daylog_activities_are_in_the_runtime_activities_list():
    """The module-level ACTIVITIES stub list is a subset; the list that
    actually reaches Temporal is built inside main(). Read it via AST."""
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

    for name in ("gather_day_events", "distil_daylog", "commit_daylog_state"):
        assert name in attrs, f"{name} missing from main()'s activities list"


def test_daylog_flow_in_schedule_map():
    from aegis_worker.schedule_sync import _ACTIVITY_TYPE_MAP

    assert "DayLogFlow" in _ACTIVITY_TYPE_MAP
    cls, config = _ACTIVITY_TYPE_MAP["DayLogFlow"](
        {"agent_id": "raphael", "config": {"day_offset": 2}}
    )
    assert cls is DayLogFlow
    assert config.agent_id == "raphael"
    assert config.day_offset == 2


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
