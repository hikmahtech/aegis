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
from aegis.services import hub_project, work_sessions
from aegis.services.hub import (
    Event,
    get_problem,
    ingest_event,
    merge_problems,
    set_service_state,
    set_status,
)
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


# --- sessions in the block (PR 5) ----------------------------------------------


def test_render_block_lists_the_sessions_on_the_task():
    p = {
        "id": "abc", "status": "open", "occurrences": 1, "first_seen_at": NOW, "last_seen_at": NOW,
        "subject": "s", "subject_kind": "service", "severity": "warning", "class": "x",
    }
    block = render_block(
        p,
        sessions=[
            {"owner": "aegis", "status": "parked", "account": "work", "last_seen_at": NOW, "summary": "waiting on you: plan"},
            {"owner": "operator", "status": "active", "account": "", "last_seen_at": None, "summary": ""},
        ],
    )
    assert "Session: aegis parked (work) · seen 2026-09-07 12:00 UTC · waiting on you: plan" in block
    assert "Session: operator active\n" in block
    assert render_block(p).count("Session:") == 0


async def test_projection_rerenders_the_block_when_a_session_registers(db_pool, inbox, todoist):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    task = (await project(db_pool, r.problem_id, now=NOW))["task_id"]
    await _mirror_task(db_pool, task)
    await work_sessions.upsert_operator_session(
        db_pool, task_id=task, account="personal", status="active", summary="checking the mounts",
        problem_id=r.problem_id,
    )
    out = await project(db_pool, r.problem_id, now=NOW)
    assert out["comments"] == 0
    cmd = await db_pool.fetchval(
        "SELECT command FROM todoist_outbox WHERE temp_id = $1", f"problem-desc-{task}"
    )
    assert cmd is not None, "a new session line is a changed block"
    assert cmd["type"] == "item_update"
    assert "Session: operator active (personal)" in cmd["args"]["description"]
    assert "checking the mounts" in cmd["args"]["description"]


# --- plan steps become subtasks (PR 5b) ----------------------------------------


def _plan(problem_id: str, steps: list[str], **kw) -> Event:
    return Event(
        source="session",
        external_id=f"plan-{uuid.uuid4().hex[:8]}",
        kind="plan",
        title="plan",
        problem_id=problem_id,
        payload={"text": "the plan", "steps": steps, **kw},
    )


def test_plan_steps_needs_two_and_caps_at_twelve():
    assert hub_project.plan_steps({}) == []
    assert hub_project.plan_steps({"steps": "not a list"}) == []
    assert hub_project.plan_steps({"steps": ["only one"]}) == [], "one step is a sentence"
    assert hub_project.plan_steps({"steps": ["a", "  ", "b"]}) == ["a", "b"]
    assert len(hub_project.plan_steps({"steps": [f"s{n}" for n in range(20)]})) == 12


async def test_a_plan_opens_one_subtask_per_step_once(db_pool, inbox, todoist):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    task = (await project(db_pool, r.problem_id, now=NOW))["task_id"]
    await _mirror_task(db_pool, task)

    await ingest_event(db_pool, _plan(r.problem_id, ["Add the index", "Backfill rows"]), now=NOW)
    out = await project(db_pool, r.problem_id, now=NOW)

    adds = [c for c in _cmds(todoist, "item_add") if c["args"].get("parent_id")]
    assert [c["args"]["content"] for c in adds] == ["Add the index", "Backfill rows"]
    assert {c["args"]["parent_id"] for c in adds} == {task}
    refs = sorted(
        r0["ref"]
        for r0 in await db_pool.fetch(
            "SELECT ref FROM problem_links WHERE problem_id = $1::uuid AND link_kind = 'plan_step'",
            r.problem_id,
        )
    )
    assert len(refs) == 2 and refs[0].startswith("1:") and refs[1].startswith("2:")
    assert out["comments"] == 1, "the plan is still commented"

    # A second plan does not open a second checklist — the first one may
    # already be half ticked off.
    await ingest_event(db_pool, _plan(r.problem_id, ["Rewrite it all", "Again"]), now=NOW)
    await project(db_pool, r.problem_id, now=NOW)
    assert len([c for c in _cmds(todoist, "item_add") if c["args"].get("parent_id")]) == 2


async def test_the_block_reports_step_progress_and_a_step_can_be_ticked_off(db_pool, inbox, todoist):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    task = (await project(db_pool, r.problem_id, now=NOW))["task_id"]
    await _mirror_task(db_pool, task)
    await ingest_event(db_pool, _plan(r.problem_id, ["one", "two"]), now=NOW)
    await project(db_pool, r.problem_id, now=NOW)
    step_ids = [
        str(row["ref"]).split(":", 1)[1]
        for row in await db_pool.fetch(
            "SELECT ref FROM problem_links WHERE problem_id = $1::uuid AND link_kind = 'plan_step' "
            "ORDER BY ref",
            r.problem_id,
        )
    ]
    for sid in step_ids:
        await _mirror_task(db_pool, sid)
    desc = await db_pool.fetchval(
        "SELECT command->'args'->>'description' FROM todoist_outbox WHERE temp_id = $1",
        f"problem-desc-{task}",
    )
    assert "Steps: 0/2 done" in (desc or "")

    # A session reports the first step finished.
    await ingest_event(
        db_pool,
        Event(source="session", external_id=f"note-{uuid.uuid4().hex[:8]}", kind="session_note",
              title="x", problem_id=r.problem_id,
              payload={"text": "index is in", "step_done": 1}),
        now=NOW,
    )
    await project(db_pool, r.problem_id, now=NOW + timedelta(minutes=1))

    assert await db_pool.fetchval(
        "SELECT is_completed FROM todoist_tasks WHERE id = $1", step_ids[0]
    ) is True
    queued = await db_pool.fetchval(
        "SELECT count(*) FROM todoist_outbox WHERE temp_id = $1", f"problem-step-{step_ids[0]}"
    )
    assert queued == 1
    desc = await db_pool.fetchval(
        "SELECT command->'args'->>'description' FROM todoist_outbox WHERE temp_id = $1",
        f"problem-desc-{task}",
    )
    assert "Steps: 1/2 done" in (desc or "")
    # A step number nobody planned is a no-op, not a crash.
    assert await hub_project._complete_step(db_pool, r.problem_id, 9) is False


async def test_a_plan_on_a_problem_with_no_task_waits(db_pool, inbox, todoist, monkeypatch):
    """No task, no subtasks: the plan is recorded and the checklist opens on
    the next projection, once the task exists."""
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    await ingest_event(db_pool, _plan(r.problem_id, ["one", "two"]), now=NOW)
    out = await project(db_pool, r.problem_id, now=NOW)
    assert out["created"] is True
    assert [c for c in _cmds(todoist, "item_add") if c["args"].get("parent_id")] == []
    await _mirror_task(db_pool, out["task_id"])
    await project(db_pool, r.problem_id, now=NOW)
    assert len([c for c in _cmds(todoist, "item_add") if c["args"].get("parent_id")]) == 0, (
        "the plan predates the task, so it is on the description, not a comment"
    )


async def test_ensure_problem_for_task_creates_one_manual_problem(db_pool, inbox, todoist):
    task = f"zze-{uuid.uuid4().hex[:6]}"
    assert await hub_project.ensure_problem_for_task(db_pool, task) is None, "unknown task"
    await _mirror_task(db_pool, task)
    await db_pool.execute("UPDATE todoist_tasks SET content = 'Fix the flaky test' WHERE id = $1", task)
    p = await hub_project.ensure_problem_for_task(db_pool, task, subject="hikmahtech/aegis")
    assert p is not None and p["class"] == "manual" and p["subject_kind"] == "repo"
    assert p["title"] == "Fix the flaky test" and p["todoist_task_id"] == task
    again = await hub_project.ensure_problem_for_task(db_pool, task)
    assert again["id"] == p["id"], "the second call finds the first one"


async def test_a_pending_description_update_is_superseded_not_dropped(db_pool, inbox, todoist):
    """The status block is written whole every time, so a newer one REPLACES a
    queued older one. The outbox otherwise leaves a pending row alone, and the
    block hash is recorded as written either way — so without superseding, the
    newer block is lost and no later projection ever queues it again.

    Falsifiable: drop `supersede=True` and the queued description stays at the
    first block.
    """
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    task = (await project(db_pool, r.problem_id, now=NOW))["task_id"]
    await _mirror_task(db_pool, task)
    await ingest_event(db_pool, _occ(s, 2), now=NOW)
    await project(db_pool, r.problem_id, now=NOW + timedelta(minutes=1))
    assert await db_pool.fetchval(
        "SELECT status FROM todoist_outbox WHERE temp_id = $1", f"problem-desc-{task}"
    ) == "pending"

    await ingest_event(db_pool, _occ(s, 3), now=NOW)
    await project(db_pool, r.problem_id, now=NOW + timedelta(hours=1))

    desc = await db_pool.fetchval(
        "SELECT command->'args'->>'description' FROM todoist_outbox WHERE temp_id = $1",
        f"problem-desc-{task}",
    )
    assert "seen 3×" in (desc or ""), "the queued row carries the newest block"


# --- a merge does not replay the duplicate's history (PR 8) --------------------


async def test_merging_a_resolved_duplicate_does_not_close_the_kept_task(db_pool, inbox, todoist):
    """The merged problem's events move to the kept one. They are history:
    replaying them posted the duplicate's whole timeline as comments, and a
    moved `resolve` completed the kept problem's task while the problem was
    still open.

    Falsifiable: drop the watermark bump in `merge_problems` and the kept task
    is completed by the next projection.
    """
    keep = await ingest_event(db_pool, _occ(_subject(), 1), now=NOW)
    keep_task = (await project(db_pool, keep.problem_id, now=NOW))["task_id"]
    await _mirror_task(db_pool, keep_task)

    dup_subject = _subject()
    dup = await ingest_event(db_pool, _occ(dup_subject, 1), now=NOW)
    await ingest_event(db_pool, _resolved(dup_subject, 2), now=NOW)
    assert (await get_problem(db_pool, dup.problem_id))["status"] == "resolved"

    await merge_problems(db_pool, keep.problem_id, dup.problem_id, by="admin", now=NOW)
    out = await project(db_pool, keep.problem_id, now=NOW + timedelta(minutes=1))

    assert out["comments"] == 0, "the duplicate's timeline is history, not news"
    assert (
        await db_pool.fetchval("SELECT is_completed FROM todoist_tasks WHERE id = $1", keep_task)
        is False
    ), "the kept problem is open; its task must stay open"
    assert (await get_problem(db_pool, keep.problem_id))["status"] == "open"


# --- a resolve closes the task whichever path reached it (PR 9) ---------------


async def test_resolving_by_status_closes_the_task_like_a_resolved_alert(db_pool, inbox, todoist):
    """`set_status(..., 'resolved')` is how an investigation and the admin
    panel's Resolve button finish a problem. It used to write `set_status`,
    which the projector ignores, so the problem read resolved while its task
    stayed open with no closing comment — the human saw nothing.

    Falsifiable: write `set_status` for a resolve again and the task stays
    open and uncommented.
    """
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    task = (await project(db_pool, r.problem_id, now=NOW))["task_id"]
    await _mirror_task(db_pool, task)

    assert await set_status(db_pool, r.problem_id, "resolved", reason="fixed by hand", now=NOW)
    out = await project(db_pool, r.problem_id, now=NOW + timedelta(minutes=1))

    assert out["comments"] == 1
    note = _cmds(todoist, "note_add")[-1]["args"]["content"]
    assert "Resolved" in note and "Closing this task" in note
    assert await db_pool.fetchval(
        "SELECT is_completed FROM todoist_tasks WHERE id = $1", task
    ) is True
    assert await db_pool.fetchval(
        "SELECT count(*) FROM todoist_outbox WHERE temp_id = $1", f"problem-close-{task}"
    ) == 1
