"""The projector: a problem's Todoist task is a view of the problem.

Real test database; the Todoist Sync API is replaced by a recorder that
accepts every command and hands back a real-looking id for an `item_add`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aegis.connectors.todoist import TodoistConnector
from aegis.services import hub_project
from aegis.services.hub import Event, get_problem, ingest_event, set_service_state
from aegis.services.hub_project import (
    COLLAPSE_WINDOW,
    FOOTER,
    merge_block,
    project,
    project_pending,
    render_block,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _subject() -> str:
    return f"svc_{uuid.uuid4().hex[:8]}"


def _occ(subject: str, n: int, **kw) -> Event:
    return Event(
        source="heartbeat",
        external_id=f"{subject}@{n}",
        kind="occurrence",
        title=f"Service {subject} down",
        klass="DockerServiceDown",
        subject=subject,
        severity="critical",
        payload={"description": f"Heartbeat saw {subject} below desired replicas."},
        occurred_at=kw.pop("occurred_at", NOW + timedelta(minutes=n)),
    )


def _resolved(subject: str, n: int) -> Event:
    return Event(
        source="heartbeat",
        external_id=f"{subject}@{n}@resolved",
        kind="resolved",
        title="recovered",
        klass="DockerServiceDown",
        subject=subject,
        occurred_at=NOW + timedelta(minutes=n),
    )


@pytest.fixture
def todoist(monkeypatch):
    """Record every Sync command; accept them all; mint an id per item_add."""
    state = {"batches": [], "fail_notes": False}

    async def fake_commands(self, commands):
        state["batches"].append(commands)
        if state["fail_notes"] and any(c["type"] == "note_add" for c in commands):
            return {"ok": False, "error": "boom", "retryable": True}
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
    # The capture path builds `Settings()` itself; there is no env here.
    monkeypatch.setattr("aegis.config.Settings", lambda: SimpleNamespace(secret_key="x"))
    return state


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


def _cmds(todoist, kind: str) -> list[dict]:
    return [c for batch in todoist["batches"] for c in batch if c["type"] == kind]


async def _mirror_task(db_pool, task_id: str, *, assignee: str = "@pandora", completed=False):
    """The sync mirror row TodoistSyncFlow would have written."""
    await db_pool.execute(
        "INSERT INTO todoist_tasks (id, project_id, content, labels, assignee_label, source_tag, "
        "is_completed, raw) VALUES ($1,'P_INBOX','t',ARRAY['#alert',$2],$2,'#alert',$3,'{}'::jsonb) "
        "ON CONFLICT (id) DO UPDATE SET assignee_label = EXCLUDED.assignee_label, "
        "is_completed = EXCLUDED.is_completed",
        task_id,
        assignee,
        completed,
    )


# --- pure helpers -------------------------------------------------------------


def test_render_and_merge_block_replace_whole_and_keep_user_text():
    p = {
        "id": "abc",
        "status": "open",
        "occurrences": 3,
        "first_seen_at": NOW,
        "last_seen_at": NOW + timedelta(hours=1),
        "subject": "monitoring_cadvisor",
        "subject_kind": "service",
        "severity": "critical",
        "class": "dockerservicedown",
    }
    block = render_block(
        p,
        window={"state": "deploying", "until_at": NOW + timedelta(minutes=20), "set_by": "ansible"},
        links=[{"link_kind": "todoist_task", "ref": "T1"}, {"link_kind": "github_pr", "ref": "o/r#5"}],
    )
    assert block.startswith("<!-- aegis:problem abc -->")
    assert "seen 3× since 2026-09-07 12:00 UTC · last 2026-09-07 13:00 UTC" in block
    assert "Window: deploying until 2026-09-07 12:20 UTC (set by ansible)" in block
    assert "Links: github_pr:o/r#5" in block and "todoist_task" not in block
    merged = merge_block("user notes\n\nmore", block)
    assert merged.startswith("user notes\n\nmore\n\n<!-- aegis:problem")
    again = merge_block(merged + "\ntrailing", block.replace("open", "fixing"))
    assert again.count("<!-- aegis:problem") == 1 and "Status: fixing" in again
    assert again.startswith("user notes") and again.endswith("trailing")
    assert merge_block(None, block) == block


# --- creating the task --------------------------------------------------------


async def test_first_projection_creates_the_task_with_block_and_label(db_pool, inbox, todoist):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    out = await project(db_pool, r.problem_id, now=NOW)
    assert out["created"] is True and out["task_id"].startswith("T")
    adds = _cmds(todoist, "item_add")
    assert len(adds) == 1
    args = adds[0]["args"]
    assert args["content"] == f"Service {s} down"
    assert args["labels"] == ["#alert", "@pandora"]
    assert "Heartbeat saw" in args["description"] and "<!-- aegis:problem" in args["description"]
    p = await get_problem(db_pool, r.problem_id)
    assert p["todoist_task_id"] == out["task_id"]
    assert p["metadata"]["projected_event_id"] > 0
    link = await db_pool.fetchval(
        "SELECT ref FROM problem_links WHERE problem_id = $1::uuid AND link_kind = 'todoist_task'",
        r.problem_id,
    )
    assert link == out["task_id"]
    # nothing to say a second time
    again = await project(db_pool, r.problem_id, now=NOW)
    assert again == {"problem_id": r.problem_id, "task_id": out["task_id"], "created": False, "comments": 0}
    assert _cmds(todoist, "note_add") == []


@pytest.mark.parametrize("status", ["suppressed", "closed"])
async def test_unprojected_statuses_get_no_task(db_pool, inbox, todoist, status):
    s = _subject()
    if status == "suppressed":
        await set_service_state(db_pool, s, "deploying", minutes=30, set_by="ansible", now=NOW)
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    if status == "closed":
        await db_pool.execute(
            "UPDATE problems SET status='closed', closed_at=$2 WHERE id=$1::uuid", r.problem_id, NOW
        )
    assert (await project(db_pool, r.problem_id, now=NOW))["skipped"] == status
    assert _cmds(todoist, "item_add") == []


async def test_muted_problem_is_left_alone(db_pool, inbox, todoist):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    await db_pool.execute(
        "UPDATE problems SET muted_until = $2 WHERE id = $1::uuid", r.problem_id, NOW + timedelta(hours=1)
    )
    assert (await project(db_pool, r.problem_id, now=NOW))["skipped"] == "muted"
    assert (await project(db_pool, r.problem_id, now=NOW + timedelta(hours=2)))["created"] is True


async def test_missing_problem(db_pool):
    assert (await project(db_pool, str(uuid.uuid4()), now=NOW))["skipped"] == "missing"


# --- comments -----------------------------------------------------------------


async def test_occurrences_collapse_into_one_comment_per_window(db_pool, inbox, todoist):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    out = await project(db_pool, r.problem_id, now=NOW)
    await _mirror_task(db_pool, out["task_id"])
    for n in (2, 3, 4):
        await ingest_event(db_pool, _occ(s, n), now=NOW)
    t1 = NOW + timedelta(minutes=5)
    out = await project(db_pool, r.problem_id, now=t1)
    assert out["comments"] == 1
    notes = _cmds(todoist, "note_add")
    assert len(notes) == 1
    assert notes[0]["args"]["content"].startswith("⚠️ 3 more occurrences (4 in total)")
    assert notes[0]["args"]["content"].endswith(FOOTER)
    # inside the window: counted, not commented
    await ingest_event(db_pool, _occ(s, 5), now=NOW)
    out = await project(db_pool, r.problem_id, now=t1 + timedelta(minutes=10))
    assert out["comments"] == 0
    assert (await get_problem(db_pool, r.problem_id))["metadata"]["pending_occurrences"] == 1
    # after the window: the pending count is reported
    out = await project(db_pool, r.problem_id, now=t1 + COLLAPSE_WINDOW)
    assert out["comments"] == 1
    assert _cmds(todoist, "note_add")[-1]["args"]["content"].startswith("⚠️ 1 more occurrence (5 in total)")


async def test_resolve_comments_and_closes_the_task(db_pool, inbox, todoist):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    out = await project(db_pool, r.problem_id, now=NOW)
    task = out["task_id"]
    await _mirror_task(db_pool, task)
    await ingest_event(db_pool, _resolved(s, 2), now=NOW)
    out = await project(db_pool, r.problem_id, now=NOW + timedelta(minutes=3))
    assert out["comments"] == 1
    assert _cmds(todoist, "note_add")[-1]["args"]["content"].startswith("✅ Resolved at 2026-09-07 12:02 UTC")
    assert await db_pool.fetchval("SELECT is_completed FROM todoist_tasks WHERE id = $1", task)
    cmd = await db_pool.fetchval(
        "SELECT command FROM todoist_outbox WHERE temp_id = $1", f"problem-close-{task}"
    )
    assert cmd["type"] == "item_complete" and cmd["args"]["id"] == task


async def test_resolve_never_closes_a_task_the_user_claimed(db_pool, inbox, todoist):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    task = (await project(db_pool, r.problem_id, now=NOW))["task_id"]
    await _mirror_task(db_pool, task, assignee="@me")
    await ingest_event(db_pool, _resolved(s, 2), now=NOW)
    await project(db_pool, r.problem_id, now=NOW)
    assert not await db_pool.fetchval("SELECT is_completed FROM todoist_tasks WHERE id = $1", task)
    assert await db_pool.fetchval(
        "SELECT count(*) FROM todoist_outbox WHERE temp_id = $1", f"problem-close-{task}"
    ) == 0


async def test_reopen_comments_and_uncompletes_the_task(db_pool, inbox, todoist):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    task = (await project(db_pool, r.problem_id, now=NOW))["task_id"]
    await _mirror_task(db_pool, task)
    await ingest_event(db_pool, _resolved(s, 2), now=NOW)
    await project(db_pool, r.problem_id, now=NOW)
    assert await db_pool.fetchval("SELECT is_completed FROM todoist_tasks WHERE id = $1", task)
    later = NOW + timedelta(hours=2)
    await ingest_event(db_pool, _occ(s, 3, occurred_at=later), now=later)
    out = await project(db_pool, r.problem_id, now=later)
    contents = [c["args"]["content"] for c in _cmds(todoist, "note_add")]
    assert any(c.startswith("🔁 Back at 2026-09-07 14:00 UTC: recurred inside the reopen window") for c in contents)
    assert out["comments"] >= 1
    assert not await db_pool.fetchval("SELECT is_completed FROM todoist_tasks WHERE id = $1", task)
    cmd = await db_pool.fetchval(
        "SELECT command FROM todoist_outbox WHERE temp_id = $1", f"problem-reopen-{task}"
    )
    assert cmd["type"] == "item_uncomplete"


async def test_promotion_after_a_deploy_window_is_explained(db_pool, inbox, todoist):
    s = _subject()
    await set_service_state(db_pool, s, "deploying", minutes=10, set_by="ansible", now=NOW)
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    assert (await project(db_pool, r.problem_id, now=NOW))["skipped"] == "suppressed"
    later = NOW + timedelta(minutes=30)
    await ingest_event(db_pool, _occ(s, 2, occurred_at=later), now=later)
    out = await project(db_pool, r.problem_id, now=later)
    # promoted → first projection creates the task; history before it is in the block, not comments
    assert out["created"] is True
    assert _cmds(todoist, "note_add") == []


async def test_history_events_comment_unless_already_posted(db_pool, inbox, todoist):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    task = (await project(db_pool, r.problem_id, now=NOW))["task_id"]
    await _mirror_task(db_pool, task)
    await ingest_event(
        db_pool,
        Event(source="investigation", external_id=f"inv-{s}", kind="investigation", title="x",
              payload={"text": "Root cause: disk full", "posted": True}, problem_id=r.problem_id),
        now=NOW,
    )
    await ingest_event(
        db_pool,
        Event(source="session", external_id=f"sess-{s}", kind="session_note", title="x",
              payload={"text": "checked mounts on noon"}, problem_id=r.problem_id),
        now=NOW,
    )
    out = await project(db_pool, r.problem_id, now=NOW)
    assert out["comments"] == 1
    assert _cmds(todoist, "note_add")[-1]["args"]["content"].startswith("💻 Session: checked mounts on noon")


async def test_failed_comment_keeps_the_watermark(db_pool, inbox, todoist):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    task = (await project(db_pool, r.problem_id, now=NOW))["task_id"]
    await _mirror_task(db_pool, task)
    await ingest_event(db_pool, _resolved(s, 2), now=NOW)
    todoist["fail_notes"] = True
    out = await project(db_pool, r.problem_id, now=NOW)
    assert out.get("partial") is True and out["comments"] == 0
    assert not await db_pool.fetchval("SELECT is_completed FROM todoist_tasks WHERE id = $1", task)
    todoist["fail_notes"] = False
    out = await project(db_pool, r.problem_id, now=NOW)
    assert out["comments"] == 1
    assert await db_pool.fetchval("SELECT is_completed FROM todoist_tasks WHERE id = $1", task)


async def test_block_is_rerendered_only_when_it_changes(db_pool, inbox, todoist):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    task = (await project(db_pool, r.problem_id, now=NOW))["task_id"]
    await _mirror_task(db_pool, task)
    await db_pool.execute(
        "UPDATE todoist_tasks SET description = 'my own notes' WHERE id = $1", task
    )
    assert await db_pool.fetchval(
        "SELECT count(*) FROM todoist_outbox WHERE temp_id = $1", f"problem-desc-{task}"
    ) == 0
    await ingest_event(db_pool, _occ(s, 2), now=NOW)
    await project(db_pool, r.problem_id, now=NOW + timedelta(minutes=1))
    cmd = await db_pool.fetchval(
        "SELECT command FROM todoist_outbox WHERE temp_id = $1", f"problem-desc-{task}"
    )
    assert cmd["type"] == "item_update"
    assert cmd["args"]["description"].startswith("my own notes\n\n<!-- aegis:problem")
    assert "seen 2×" in cmd["args"]["description"]


async def test_outbox_temp_task_waits_for_the_real_id(db_pool, inbox, todoist):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    await db_pool.execute(
        "UPDATE problems SET todoist_task_id = 'item-temp' WHERE id = $1::uuid", r.problem_id
    )
    assert (await project(db_pool, r.problem_id, now=NOW))["skipped"] == "task_pending_outbox"
    await db_pool.execute(
        "INSERT INTO todoist_capture_idempotency (source_tag, external_id, todoist_task_ref) "
        "VALUES ('#alert', $1, 'T_REAL') ON CONFLICT (source_tag, external_id) "
        "DO UPDATE SET todoist_task_ref = EXCLUDED.todoist_task_ref",
        f"problem-{r.problem_id}",
    )
    out = await project(db_pool, r.problem_id, now=NOW)
    assert out["task_id"] == "T_REAL" and out["created"] is False
    assert (await get_problem(db_pool, r.problem_id))["todoist_task_id"] == "T_REAL"


async def test_no_todoist_means_no_task_and_no_watermark(db_pool, inbox, monkeypatch):
    async def no_key(pool, settings):
        return ""

    monkeypatch.setattr("aegis.services.tools.gtd.resolve_todoist_api_key", no_key)
    monkeypatch.setattr("aegis.config.Settings", lambda: SimpleNamespace(secret_key="x"))
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    assert (await project(db_pool, r.problem_id, now=NOW))["skipped"] == "no_task"
    assert (await get_problem(db_pool, r.problem_id))["metadata"] == {}


# --- the sweep ----------------------------------------------------------------


async def test_project_pending_picks_missing_behind_and_temp_tasks_only(db_pool, inbox, todoist):
    a, b, c = _subject(), _subject(), _subject()
    ra = await ingest_event(db_pool, _occ(a, 1), now=NOW)  # no task yet
    rb = await ingest_event(db_pool, _occ(b, 1), now=NOW)
    await project(db_pool, rb.problem_id, now=NOW)  # up to date
    rc = await ingest_event(db_pool, _occ(c, 1), now=NOW)
    task_c = (await project(db_pool, rc.problem_id, now=NOW))["task_id"]
    await _mirror_task(db_pool, task_c)
    await ingest_event(db_pool, _occ(c, 2), now=NOW)  # behind
    results = await project_pending(db_pool, now=NOW + timedelta(minutes=1))
    ids = {r["problem_id"] for r in results}
    assert ra.problem_id in ids and rc.problem_id in ids and rb.problem_id not in ids
    assert not any("error" in r for r in results)


async def test_project_pending_survives_one_bad_problem(db_pool, inbox, todoist, monkeypatch):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    real = hub_project.project

    async def boom(pool, problem_id, **kw):
        if problem_id == r.problem_id:
            raise RuntimeError("todoist exploded")
        return await real(pool, problem_id, **kw)

    monkeypatch.setattr(hub_project, "project", boom)
    results = await project_pending(db_pool, now=NOW)
    mine = [x for x in results if x["problem_id"] == r.problem_id]
    assert mine and mine[0]["error"].startswith("todoist exploded")
