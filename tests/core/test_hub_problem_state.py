"""Who decides what a problem's record says: its status and its severity.

Pinned against the real test database (#484's late-verdict rule left with
the alert investigations, for the DevOps vertical):

* **#486 — a problem is as bad as its worst occurrence.** A later, worse
  occurrence raises the stored severity (and so the task's status block); a
  milder one never lowers it.

The Todoist Sync API is replaced by a recorder, as in `test_hub_project.py`,
so the projector's outbox writes are the thing asserted on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aegis.connectors.todoist import TodoistConnector
from aegis.services.hub import (
    Event,
    get_problem,
    ingest_event,
    list_events,
)
from aegis.services.hub_project import project

pytestmark = pytest.mark.asyncio

# Problem 2140a366 in prod: aegis_comms crash-looping on 2026-09-08.
ALERT_AT = datetime(2026, 9, 8, 10, 12, tzinfo=UTC)
INVESTIGATING_AT = ALERT_AT + timedelta(minutes=3)
CLEARED_AT = ALERT_AT + timedelta(minutes=5)
VERDICT_AT = ALERT_AT + timedelta(minutes=8, seconds=40)


def _subject() -> str:
    return f"svc_{uuid.uuid4().hex[:8]}"


def _occ(subject: str, n: int, at: datetime, *, severity: str = "warning", **kw) -> Event:
    return Event(
        source="flow_health",
        external_id=f"{subject}@{n}",
        kind="occurrence",
        title=f"ServiceCrashLooping: {subject}",
        klass="ServiceCrashLooping",
        subject=subject,
        severity=severity,
        occurred_at=at,
        **kw,
    )


def _cleared(subject: str, n: int, at: datetime) -> Event:
    return Event(
        source="flow_health",
        external_id=f"{subject}@{n}@resolved",
        kind="resolved",
        title=f"ServiceCrashLooping: {subject}",
        klass="ServiceCrashLooping",
        subject=subject,
        occurred_at=at,
    )


async def _state_changes(pool, problem_id: str) -> list[dict]:
    """The problem's `state_change` payloads, oldest first."""
    events = await list_events(pool, problem_id, limit=200)
    return [e["payload"] for e in reversed(events) if e["kind"] == "state_change"]


# --- a recorder in place of Todoist -----------------------------------------------


@pytest.fixture
def todoist(monkeypatch):
    """Accept every Sync command and mint an id for each `item_add`."""
    batches: list[list[dict]] = []

    async def fake_commands(self, commands):
        batches.append(commands)
        mapping = {c["temp_id"]: f"T{uuid.uuid4().hex[:10]}" for c in commands if "temp_id" in c}
        return {
            "ok": True,
            "data": {"sync_status": {c["uuid"]: "ok" for c in commands}, "temp_id_mapping": mapping},
        }

    async def fake_key(pool, settings):
        return "test-key"

    async def _close(self):
        return None

    monkeypatch.setattr(TodoistConnector, "commands", fake_commands)
    monkeypatch.setattr(TodoistConnector, "close", _close)
    monkeypatch.setattr("aegis.services.hub_project.resolve_todoist_api_key", fake_key)
    monkeypatch.setattr("aegis.services.tools.gtd.resolve_todoist_api_key", fake_key)
    monkeypatch.setattr("aegis.config.Settings", lambda: SimpleNamespace(secret_key="x"))
    return batches


@pytest_asyncio.fixture(loop_scope="function")
async def inbox(db_pool):
    await db_pool.execute(
        "INSERT INTO settings (key, value) VALUES ('todoist_managed_project_ids', $1) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        {"inbox": "P_INBOX"},
    )
    await db_pool.execute(
        "INSERT INTO todoist_projects (id, name, is_managed, raw) "
        "VALUES ('P_INBOX','Inbox',true,'{}'::jsonb) ON CONFLICT (id) DO NOTHING"
    )


async def _mirror_task(pool, task_id: str) -> None:
    """The sync mirror row TodoistSyncFlow would have written."""
    await pool.execute(
        "INSERT INTO todoist_tasks (id, project_id, content, labels, assignee_label, source_tag, "
        "is_completed, raw) VALUES ($1,'P_INBOX','t',ARRAY['#alert','@sebas'],'@sebas',"
        "'#alert',false,'{}'::jsonb) ON CONFLICT (id) DO NOTHING",
        task_id,
    )


async def _completed(pool, task_id: str) -> bool:
    return await pool.fetchval("SELECT is_completed FROM todoist_tasks WHERE id = $1", task_id)


async def _queued(pool, temp_id: str) -> dict | None:
    return await pool.fetchval("SELECT command FROM todoist_outbox WHERE temp_id = $1", temp_id)


# --- #484: an investigation annotates; the alert source decides liveness --------


# --- #486: a problem is as bad as its worst occurrence --------------------------


async def test_a_worse_occurrence_raises_severity_and_a_milder_one_never_lowers_it(db_pool):
    """A cert opens at 14 days as `warning`; at 7 days each daily occurrence
    is `critical`. The problem row used to keep `warning` for good, because
    only the create path wrote severity.

    Falsifiable: drop the severity write from the attach path and the problem
    stays `warning` after the critical occurrence.
    """
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1, ALERT_AT, severity="warning"), now=ALERT_AT)
    assert (await get_problem(db_pool, r.problem_id))["severity"] == "warning"

    at = ALERT_AT + timedelta(days=7)
    worse = await ingest_event(db_pool, _occ(s, 2, at, severity="critical"), now=at)
    assert worse.action == "attached"
    assert (await get_problem(db_pool, r.problem_id))["severity"] == "critical"

    # Milder occurrences never lower it: a problem that was once critical
    # does not read as fine because one later occurrence was gentler.
    for n, sev in ((3, "warning"), (4, "info"), (5, "error")):
        at += timedelta(days=1)
        await ingest_event(db_pool, _occ(s, n, at, severity=sev), now=at)
    assert (await get_problem(db_pool, r.problem_id))["severity"] == "critical"
    # Each occurrence still keeps its own severity.
    occ = [e["severity"] for e in await list_events(db_pool, r.problem_id) if e["kind"] == "occurrence"]
    assert occ == ["error", "info", "warning", "critical", "warning"]


async def test_severity_uses_the_hubs_normalisation_and_ordering(db_pool):
    """`fatal` is `critical` and `warn` is `warning` — the same words
    `normalize_severity` maps everywhere else — and `error` sits between
    `warning` and `critical`."""
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1, ALERT_AT, severity="info"), now=ALERT_AT)
    await ingest_event(db_pool, _occ(s, 2, ALERT_AT, severity="warn"), now=ALERT_AT)
    assert (await get_problem(db_pool, r.problem_id))["severity"] == "warning"
    await ingest_event(db_pool, _occ(s, 3, ALERT_AT, severity="error"), now=ALERT_AT)
    assert (await get_problem(db_pool, r.problem_id))["severity"] == "error"
    await ingest_event(db_pool, _occ(s, 4, ALERT_AT, severity="fatal"), now=ALERT_AT)
    assert (await get_problem(db_pool, r.problem_id))["severity"] == "critical"


async def test_a_worse_return_raises_severity_on_reopen(db_pool):
    """A problem that comes back worse inside the reopen window is worse."""
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1, ALERT_AT, severity="warning"), now=ALERT_AT)
    await ingest_event(db_pool, _cleared(s, 1, CLEARED_AT), now=CLEARED_AT)
    back_at = CLEARED_AT + timedelta(hours=1)
    back = await ingest_event(db_pool, _occ(s, 2, back_at, severity="critical"), now=back_at)
    assert back.action == "reopened"
    assert (await get_problem(db_pool, r.problem_id))["severity"] == "critical"



async def test_only_an_occurrence_raises_severity(db_pool):
    """A resolution, an investigation report or a note is not the problem
    getting worse. An investigation event carries the default `warning`, so
    letting any event raise severity would turn every `info` problem into a
    `warning` the moment it was investigated."""
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1, ALERT_AT, severity="info"), now=ALERT_AT)
    await ingest_event(
        db_pool,
        Event(
            source="session",
            external_id=f"inv-{s}",
            kind="investigation",
            title="looked",
            problem_id=r.problem_id,
            occurred_at=ALERT_AT,
        ),
        now=ALERT_AT,
    )
    await ingest_event(
        db_pool,
        Event(
            source="flow_health",
            external_id=f"{s}@1@resolved",
            kind="resolved",
            title="recovered",
            klass="ServiceCrashLooping",
            subject=s,
            severity="critical",
            occurred_at=CLEARED_AT,
        ),
        now=CLEARED_AT,
    )
    assert (await get_problem(db_pool, r.problem_id))["severity"] == "info"


async def test_the_status_block_shows_the_raised_severity(db_pool, inbox, todoist):
    """The status block prints `problem['severity']`, and the projector
    re-renders it whenever its content changes, so raising the row is what
    puts `critical` on the task."""
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1, ALERT_AT, severity="warning"), now=ALERT_AT)
    task = (await project(db_pool, r.problem_id, now=ALERT_AT))["task_id"]
    await _mirror_task(db_pool, task)
    assert await _queued(db_pool, f"problem-desc-{task}") is None

    at = ALERT_AT + timedelta(days=7)
    await ingest_event(db_pool, _occ(s, 2, at, severity="critical"), now=at)
    await project(db_pool, r.problem_id, now=at)

    cmd = await _queued(db_pool, f"problem-desc-{task}")
    assert cmd is not None and cmd["type"] == "item_update"
    assert f"Subject: {s} (service) · critical ·" in cmd["args"]["description"]
