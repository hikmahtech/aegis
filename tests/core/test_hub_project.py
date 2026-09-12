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
    list_events,
    merge_problems,
    mute_problem,
    set_service_state,
    set_status,
)
from aegis.services.hub_group import upgrade
from aegis.services.hub_project import (
    COLLAPSE_WINDOW,
    FOOTER,
    MONEY_SOURCE_TAG,
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
    state = {"batches": [], "fail_notes": False, "fail_adds": False}

    async def fake_commands(self, commands):
        state["batches"].append(commands)
        if state["fail_notes"] and any(c["type"] == "note_add" for c in commands):
            return {"ok": False, "error": "boom", "retryable": True}
        if state["fail_adds"] and any(c["type"] == "item_add" for c in commands):
            return {"ok": False, "error": "503", "retryable": True}
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
    # This file is about what a task looks like, not about whether a blip has
    # earned one, and every problem here is minted seconds before it is
    # projected. So the settle window is off (#537); the four tests at the
    # bottom of the file turn it back on and own that behaviour.
    await db_pool.execute(
        "INSERT INTO settings (key, value) VALUES ('hub_settle_seconds', $1) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        {"*": 0},
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


_BOOKS_PROJECTS = "integration:books_todoist_projects"


def _money(subject: str, n: int = 1) -> Event:
    """A reconciliation finding as `hub_watch.reconcile_findings` ingests it."""
    return Event(
        source="money",
        external_id=f"money:unmatched_rows:{subject}@{n}",
        kind="occurrence",
        title=f"3 unmatched rows on {subject}",
        klass="unmatched_rows",
        subject=subject,
        subject_kind="instrument",
        payload={"rows": 3, "description": "Three rows on this account match nothing."},
        occurred_at=NOW + timedelta(minutes=n),
    )


@pytest_asyncio.fixture(loop_scope="function")
async def books_projects(db_pool):
    """The row the admin Integrations page writes, spelled as loosely as a
    person types it."""
    await db_pool.execute(
        "INSERT INTO settings (key, value) VALUES ($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        _BOOKS_PROJECTS,
        {"val": " personal = P_FINANCE ,hikmah=P_HIKMAH"},
    )
    yield
    await db_pool.execute("DELETE FROM settings WHERE key = $1", _BOOKS_PROJECTS)


async def test_a_money_problem_is_maous_task_in_the_personal_books_project(
    db_pool, inbox, todoist, books_projects
):
    """All 13 money problems in prod (2026-09-11) were projected as
    `#alert @pandora` in the Inbox, and the agent sweep then ran Pandora's
    infra verb on them. A problem first raised by the money lane belongs to
    the finance agent; every other source keeps the infra agent and the Inbox.

    Falsifiable: route every problem to the infra owner and the money task is
    `#alert @pandora` in the Inbox again.
    """
    inst = f"zz-acct-{uuid.uuid4().hex[:8]}"
    r = await ingest_event(db_pool, _money(inst), now=NOW)
    out = await project(db_pool, r.problem_id, now=NOW)
    assert out["created"] is True

    args = _cmds(todoist, "item_add")[0]["args"]
    # `#money` first: the mirror takes the first `#` label as the source tag.
    assert args["labels"] == ["#money", "@maou", "@next"]
    assert args["project_id"] == "P_FINANCE"
    assert "Three rows on this account" in args["description"]
    assert await db_pool.fetchval(
        "SELECT todoist_task_ref FROM todoist_capture_idempotency "
        "WHERE source_tag = '#money' AND external_id = $1",
        f"problem-{r.problem_id}",
    ) == out["task_id"]

    # The same setting leaves an alert exactly where it always went.
    s = _subject()
    alert = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    await project(db_pool, alert.problem_id, now=NOW)
    alert_args = _cmds(todoist, "item_add")[1]["args"]
    assert alert_args["labels"] == ["#alert", "@pandora"]
    assert alert_args["project_id"] == "P_INBOX"


async def test_a_money_task_follows_the_env_then_the_inbox(db_pool, inbox, todoist, monkeypatch):
    """No DB row: the env value. Neither: the Inbox, as before."""
    await db_pool.execute("DELETE FROM settings WHERE key = $1", _BOOKS_PROJECTS)
    monkeypatch.setattr(
        "aegis.config.Settings",
        lambda: SimpleNamespace(secret_key="x", books_todoist_projects="personal=P_ENV"),
    )
    first = await ingest_event(db_pool, _money(f"zz-acct-{uuid.uuid4().hex[:8]}"), now=NOW)
    await project(db_pool, first.problem_id, now=NOW)
    assert _cmds(todoist, "item_add")[0]["args"]["project_id"] == "P_ENV"

    monkeypatch.setattr("aegis.config.Settings", lambda: SimpleNamespace(secret_key="x"))
    second = await ingest_event(db_pool, _money(f"zz-acct-{uuid.uuid4().hex[:8]}"), now=NOW)
    await project(db_pool, second.problem_id, now=NOW)
    args = _cmds(todoist, "item_add")[1]["args"]
    assert args["project_id"] == "P_INBOX"
    assert args["labels"] == ["#money", "@maou", "@next"]


async def test_the_money_label_is_the_finance_agents_own_alias(db_pool, inbox, todoist):
    """The label comes from whoever holds the `finance` tag, not a literal:
    rename the agent's alias and the task follows it."""
    agent = await db_pool.fetchval(
        "SELECT id FROM agents WHERE active AND capabilities ? 'finance' ORDER BY id LIMIT 1"
    )
    meta = await db_pool.fetchval("SELECT metadata FROM agents WHERE id = $1", agent)
    await db_pool.execute(
        "UPDATE agents SET metadata = $2 WHERE id = $1",
        agent,
        {**(meta or {}), "mention_aliases": ["ledgerbot"]},
    )
    try:
        r = await ingest_event(db_pool, _money(f"zz-acct-{uuid.uuid4().hex[:8]}"), now=NOW)
        await project(db_pool, r.problem_id, now=NOW)
        assert _cmds(todoist, "item_add")[0]["args"]["labels"] == ["#money", "@ledgerbot", "@next"]
    finally:
        await db_pool.execute("UPDATE agents SET metadata = $2 WHERE id = $1", agent, meta or {})


async def test_a_money_task_in_the_outbox_is_found_under_its_own_tag(db_pool, inbox, todoist):
    """The capture idempotency row is keyed on the tag the task was captured
    with. Looked up under `#alert`, an outbox-created money task would never
    learn its real id.

    Falsifiable: look the ref up under `SOURCE_TAG` and the projection still
    says `task_pending_outbox`.
    """
    r = await ingest_event(db_pool, _money(f"zz-acct-{uuid.uuid4().hex[:8]}"), now=NOW)
    await db_pool.execute(
        "UPDATE problems SET todoist_task_id = 'item-money-temp' WHERE id = $1::uuid", r.problem_id
    )
    await db_pool.execute(
        "INSERT INTO todoist_capture_idempotency (source_tag, external_id, todoist_task_ref) "
        "VALUES ('#money', $1, 'T_MONEY_REAL') ON CONFLICT (source_tag, external_id) "
        "DO UPDATE SET todoist_task_ref = EXCLUDED.todoist_task_ref",
        f"problem-{r.problem_id}",
    )
    out = await project(db_pool, r.problem_id, now=NOW)
    assert out["task_id"] == "T_MONEY_REAL"
    assert (await get_problem(db_pool, r.problem_id))["todoist_task_id"] == "T_MONEY_REAL"


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


# --- a problem and its task stay in step (#473) --------------------------------


async def _complete_by_hand(db_pool, task_id: str) -> None:
    """What TodoistSyncFlow writes when a person ticks the task off in Todoist:
    the item comes back `checked`, so the mirror row turns completed."""
    await db_pool.execute(
        "UPDATE todoist_tasks SET is_completed = true, completed_at = now(), updated_at = now() "
        "WHERE id = $1",
        task_id,
    )


async def _is_completed(db_pool, task_id: str) -> bool:
    return bool(
        await db_pool.fetchval("SELECT is_completed FROM todoist_tasks WHERE id = $1", task_id)
    )


def _notes_on(todoist, task_id: str) -> list[str]:
    """Comment texts posted on one task, footer stripped."""
    return [
        c["args"]["content"].removesuffix(FOOTER)
        for c in _cmds(todoist, "note_add")
        if c["args"]["item_id"] == task_id
    ]


async def _resolved_by_task(db_pool, now) -> list[str]:
    return [
        r["problem_id"]
        for r in await hub_project.reconcile_completed_tasks(db_pool, now=now)
        if r["action"] == "resolved"
    ]


async def test_a_task_a_person_completes_resolves_its_problem(db_pool, inbox, todoist):
    """Prod on 2026-09-11 had three `waiting_human` problems whose tasks were
    completed on 09-08. Nothing read the completion back, and the heartbeat's
    stale-problem query skips `waiting_human`, so they counted as open on the
    Problems page and in the digest for good — and a new occurrence on one
    of them would have attached to a closed task in silence.

    Falsifiable: skip the `set_status` in `reconcile_completed_tasks` and the
    problem stays `waiting_human`.
    """
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    task = (await project(db_pool, r.problem_id, now=NOW))["task_id"]
    await _mirror_task(db_pool, task)
    assert await set_status(db_pool, r.problem_id, "waiting_human", reason="gate 2 is open", now=NOW)
    await project(db_pool, r.problem_id, now=NOW)
    await _complete_by_hand(db_pool, task)

    later = NOW + timedelta(minutes=5)
    assert r.problem_id in await _resolved_by_task(db_pool, later)

    p = await get_problem(db_pool, r.problem_id)
    assert p["status"] == "resolved" and p["resolved_at"] == later
    change = [e for e in await list_events(db_pool, r.problem_id) if e["kind"] == "state_change"][0]
    assert change["payload"]["action"] == "resolve", "the word the projector acts on"
    assert change["payload"]["reason"] == hub_project.TASK_COMPLETED_REASON

    # The projector records it on the task once, and has nothing to close.
    out = await project(db_pool, r.problem_id, now=later)
    assert out["comments"] == 1
    assert _notes_on(todoist, task)[-1] == (
        "✅ Resolved at 2026-09-07 12:05 UTC. This task was already completed."
    )
    assert await db_pool.fetchval(
        "SELECT count(*) FROM todoist_outbox WHERE temp_id = $1", f"problem-close-{task}"
    ) == 0

    # A second sweep finds nothing left to do.
    assert r.problem_id not in await _resolved_by_task(db_pool, later)


async def test_a_problem_that_comes_back_after_a_person_completed_it_reopens_the_task(
    db_pool, inbox, todoist
):
    """Completing the task is the person's word that it is over. If it is not,
    the next occurrence reopens the problem through the ordinary path, and the
    projector un-completes the task, so the return is not lost on a closed one."""
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    task = (await project(db_pool, r.problem_id, now=NOW))["task_id"]
    await _mirror_task(db_pool, task)
    await _complete_by_hand(db_pool, task)
    done_at = NOW + timedelta(minutes=5)
    assert r.problem_id in await _resolved_by_task(db_pool, done_at)
    await project(db_pool, r.problem_id, now=done_at)

    back = NOW + timedelta(hours=1)
    again = await ingest_event(db_pool, _occ(s, 2, occurred_at=back), now=back)
    assert again.problem_id == r.problem_id and again.action == "reopened"
    await project(db_pool, r.problem_id, now=back)

    assert (
        "🔁 Back at 2026-09-07 13:00 UTC: recurred inside the reopen window."
        in _notes_on(todoist, task)
    )
    assert not await _is_completed(db_pool, task)
    cmd = await db_pool.fetchval(
        "SELECT command FROM todoist_outbox WHERE temp_id = $1", f"problem-reopen-{task}"
    )
    assert cmd["type"] == "item_uncomplete" and cmd["args"]["id"] == task


async def test_a_task_the_hub_closed_is_never_taken_for_a_person(db_pool, inbox, todoist):
    """The hub completes a task only when its problem resolves — but the
    problem can come back while the projector is not allowed to say so yet:
    under a mute, or inside a deploy window. Then it is live, its task is the
    one the HUB closed, and the reopen waits behind the watermark. Resolving
    it would swallow the return; inside a window, the failure the deploy
    caused would never be promoted at all.

    Falsifiable: drop the pending-reopen clause from the predicate and both
    problems are resolved.
    """
    muted, windowed = _subject(), _subject()
    ids, tasks = {}, {}
    for s in (muted, windowed):
        r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
        task = (await project(db_pool, r.problem_id, now=NOW))["task_id"]
        await _mirror_task(db_pool, task)
        await ingest_event(db_pool, _resolved(s, 2), now=NOW)
        await project(db_pool, r.problem_id, now=NOW)
        assert await _is_completed(db_pool, task), "the hub closed it on the resolve"
        ids[s], tasks[s] = r.problem_id, task

    back = NOW + timedelta(hours=1)
    await mute_problem(db_pool, ids[muted], hours=24, by="gate2", now=NOW)
    await ingest_event(db_pool, _occ(muted, 3, occurred_at=back), now=back)
    assert (await project(db_pool, ids[muted], now=back))["skipped"] == "muted"

    await set_service_state(db_pool, windowed, "deploying", minutes=30, set_by="ansible", now=back)
    await ingest_event(db_pool, _occ(windowed, 3, occurred_at=back), now=back)
    assert (await get_problem(db_pool, ids[windowed]))["status"] == "suppressed"

    touched = {
        r["problem_id"]
        for r in await hub_project.reconcile_completed_tasks(
            db_pool, now=back + timedelta(minutes=5)
        )
    }
    assert ids[muted] not in touched and ids[windowed] not in touched
    assert (await get_problem(db_pool, ids[muted]))["status"] == "open"
    assert (await get_problem(db_pool, ids[windowed]))["status"] == "suppressed"
    # Nor are the tasks reopened behind the mute's or the window's back: the
    # projector does that when it may.
    assert await _is_completed(db_pool, tasks[muted])
    assert await _is_completed(db_pool, tasks[windowed])


async def test_the_hubs_own_close_from_before_a_return_is_undone_not_resolved(
    db_pool, inbox, todoist
):
    """Prod 2140a366, 2026-09-08, step by step. The alert resolved and the
    hub queued the close. TodoistSyncFlow applies Todoist's changes BEFORE
    it drains the outbox, so its next tick first wrote the task back as open
    in the mirror, then sent the close. Forty seconds later an investigation
    moved the problem to `waiting_human` — a return — and the projector's
    reopen read that stale "open" and sent nothing. The next diff brought
    the close in as "completed", and the task stayed closed on a live
    problem for three days. The issue took it for a person's completion.

    The completion is older than the return, so it is the hub's own close:
    the task is reopened, as the projector meant, and the problem stays
    live. A person completing it afterwards still resolves it.

    Since #484 a late verdict no longer reopens a resolved problem, so the
    return here is what can still bring one back: a real occurrence. The
    stale mirror races any return the same way.

    Falsifiable: treat every completion as a person's and the problem is
    resolved on the hub's own close.
    """
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    task = (await project(db_pool, r.problem_id, now=NOW))["task_id"]
    await _mirror_task(db_pool, task)
    await ingest_event(db_pool, _resolved(s, 2), now=NOW)
    await project(db_pool, r.problem_id, now=NOW)  # the hub queues its close
    # The sync's apply, before its drain: Todoist has not closed it yet.
    await db_pool.execute("UPDATE todoist_tasks SET is_completed = false WHERE id = $1", task)
    closed_at = await db_pool.fetchval("SELECT clock_timestamp()")  # the drain closes it

    back = await ingest_event(db_pool, _occ(s, 3), now=NOW + timedelta(minutes=3))
    assert back.action == "reopened", "the return"
    await project(db_pool, r.problem_id, now=NOW + timedelta(minutes=3))
    assert await db_pool.fetchval(
        "SELECT count(*) FROM todoist_outbox WHERE temp_id = $1", f"problem-reopen-{task}"
    ) == 0, "the stale mirror said open, so the reopen sent nothing"

    # The next diff brings the close in, stamped before the return.
    await db_pool.execute(
        "UPDATE todoist_tasks SET is_completed = true, completed_at = $2 WHERE id = $1",
        task,
        closed_at,
    )
    done = await hub_project.reconcile_completed_tasks(db_pool, now=NOW + timedelta(minutes=5))

    assert {"problem_id": r.problem_id, "task_id": task, "was": "open",
            "action": "task_reopened"} in done
    assert (await get_problem(db_pool, r.problem_id))["status"] == "open"
    assert not await _is_completed(db_pool, task)
    cmd = await db_pool.fetchval(
        "SELECT command FROM todoist_outbox WHERE temp_id = $1", f"problem-reopen-{task}"
    )
    assert cmd["type"] == "item_uncomplete" and cmd["args"]["id"] == task

    # A person completing it after the return is their word on it.
    await db_pool.execute(
        "UPDATE todoist_tasks SET is_completed = true, completed_at = clock_timestamp() "
        "WHERE id = $1",
        task,
    )
    assert r.problem_id in await _resolved_by_task(db_pool, NOW + timedelta(minutes=10))


async def test_a_task_still_in_the_outbox_is_left_alone(db_pool, inbox, todoist):
    """An `item-…` ref is a capture the sync has not drained yet: there is no
    real task behind it to have been completed. The mirror holds no row for a
    temp id today; this is the row a future placeholder would add."""
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    temp = f"item-{s}"
    await db_pool.execute(
        "UPDATE problems SET todoist_task_id = $2 WHERE id = $1::uuid", r.problem_id, temp
    )
    await _mirror_task(db_pool, temp, completed=True)
    assert r.problem_id not in await _resolved_by_task(db_pool, NOW)
    assert (await get_problem(db_pool, r.problem_id))["status"] == "open"


async def test_a_hand_written_task_and_a_group_follow_the_same_rule(db_pool, inbox, todoist):
    """A `manual` problem IS a hand-written `@code` task, so a person finishing
    the task finishes the problem. A group is one condition behind one task:
    completing that task resolves it, and the next member's occurrence reopens
    it and its task, the way it would any other problem."""
    code_task = f"zzm-{uuid.uuid4().hex[:6]}"
    await _mirror_task(db_pool, code_task)
    manual = await hub_project.ensure_problem_for_task(db_pool, code_task, subject="o/r")
    assert manual is not None and manual["class"] == "manual"

    klass = f"zzgrp{uuid.uuid4().hex[:8]}"

    def stuck(subject: str, at: datetime) -> Event:
        return Event(
            source="social",
            external_id=f"social:{klass}:{subject}@{at.isoformat()}",
            kind="occurrence",
            title=f"Post {subject} stuck",
            subject=subject,
            subject_kind="post",
            klass=klass,
            occurred_at=at,
        )

    members = []
    for subject in ("a", "b", "c"):
        r = await ingest_event(db_pool, stuck(subject, NOW), now=NOW)
        await _mirror_task(db_pool, (await project(db_pool, r.problem_id, now=NOW))["task_id"])
        members.append(r.problem_id)
    group = await upgrade(
        db_pool, klass=klass, subject_kind="post", title="3 posts stuck",
        member_ids=members, by="test", now=NOW,
    )
    await project(db_pool, group["problem_id"], now=NOW)
    group_task = (await get_problem(db_pool, group["problem_id"]))["todoist_task_id"]

    await _complete_by_hand(db_pool, code_task)
    await _complete_by_hand(db_pool, group_task)
    later = NOW + timedelta(minutes=5)
    resolved = await _resolved_by_task(db_pool, later)
    assert manual["id"] in resolved and group["problem_id"] in resolved
    await project(db_pool, group["problem_id"], now=later)

    back = NOW + timedelta(hours=1)
    fourth = await ingest_event(db_pool, stuck("d", back), now=back)
    assert fourth.problem_id == group["problem_id"] and fourth.action == "reopened"
    await project(db_pool, group["problem_id"], now=back)
    assert not await _is_completed(db_pool, group_task)


async def test_a_problem_that_resolves_while_muted_still_closes_its_task(db_pool, inbox, todoist):
    """Prod on 2026-09-11: 586cbacb and 6f15709b resolved on 09-10 under a mute
    that ran to 09-11 15:14, and their tasks stayed open the whole time. A
    mute that outlasted `problem_close_days` would have let the close sweep
    retire the problem first, and the task would never have closed at all.

    A mute silences what a problem DOES, not its recovery: the sweep still
    closes the task, and the occurrences stay silent.

    Falsifiable: put back the sweep's mute filter (or the projector's muted
    skip) and the task stays open.
    """
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    task = (await project(db_pool, r.problem_id, now=NOW))["task_id"]
    await _mirror_task(db_pool, task)
    await mute_problem(db_pool, r.problem_id, hours=24, by="gate2", now=NOW)
    for n in (2, 3):
        await ingest_event(db_pool, _occ(s, n), now=NOW)
    await ingest_event(db_pool, _resolved(s, 4), now=NOW)

    results = await project_pending(db_pool, now=NOW + timedelta(minutes=10))

    mine = [x for x in results if x["problem_id"] == r.problem_id]
    assert mine and mine[0].get("comments") == 1
    assert _notes_on(todoist, task) == ["✅ Resolved at 2026-09-07 12:04 UTC. Closing this task."]
    assert await _is_completed(db_pool, task)
    p = await get_problem(db_pool, r.problem_id)
    assert p["metadata"]["pending_occurrences"] == 0, "the mute swallowed them"
    latest = await db_pool.fetchval(
        "SELECT max(id) FROM problem_events WHERE problem_id = $1::uuid", r.problem_id
    )
    assert p["metadata"]["projected_event_id"] == latest


async def test_a_problem_flapping_under_a_mute_closes_with_one_comment(db_pool, inbox, todoist):
    """586cbacb's real backlog behind its mute: five resolves, four reopens and
    six occurrences. Told turn by turn that is ten comments the moment the
    projector reaches it. A batch is told as where it ends, once.

    Falsifiable: comment on every resolve and reopen again and this batch
    posts five comments.
    """
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    task = (await project(db_pool, r.problem_id, now=NOW))["task_id"]
    await _mirror_task(db_pool, task)
    await mute_problem(db_pool, r.problem_id, hours=24, by="gate2", now=NOW)
    for n in (2, 3, 4, 5, 6):
        at = NOW + timedelta(minutes=n)
        event = _resolved(s, n) if n % 2 == 0 else _occ(s, n, occurred_at=at)
        await ingest_event(db_pool, event, now=at)

    out = await project(db_pool, r.problem_id, now=NOW + timedelta(minutes=10))

    assert out["comments"] == 1
    assert _notes_on(todoist, task) == [
        "✅ Resolved at 2026-09-07 12:06 UTC. Closing this task. "
        "It came back twice since the last update."
    ]
    assert await _is_completed(db_pool, task)


async def test_a_return_under_a_mute_waits_for_the_mute_to_end(db_pool, inbox, todoist):
    """A mute means "stop telling me about this until then", and reopening the
    task would put it back on the person's list, which is telling them. So a
    problem that comes back under a mute stays quiet — the problem row says
    open, the Problems page and the digest show it — and the task reopens,
    with one comment, when the mute ends."""
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    task = (await project(db_pool, r.problem_id, now=NOW))["task_id"]
    await _mirror_task(db_pool, task)
    await ingest_event(db_pool, _resolved(s, 2), now=NOW)
    await project(db_pool, r.problem_id, now=NOW)
    await mute_problem(db_pool, r.problem_id, hours=2, by="gate2", now=NOW)

    back = NOW + timedelta(minutes=30)
    await ingest_event(db_pool, _occ(s, 3, occurred_at=back), now=back)
    before = len(_notes_on(todoist, task))
    assert (await project(db_pool, r.problem_id, now=back))["skipped"] == "muted"
    assert len(_notes_on(todoist, task)) == before
    assert await _is_completed(db_pool, task), "still closed while the mute holds"

    ended = NOW + timedelta(hours=3)
    assert r.problem_id in {x["problem_id"] for x in await project_pending(db_pool, now=ended)}
    assert (
        "🔁 Back at 2026-09-07 12:30 UTC: recurred inside the reopen window."
        in _notes_on(todoist, task)
    )
    assert not await _is_completed(db_pool, task)


async def test_a_backlog_of_turns_is_told_as_where_it_ends(db_pool, inbox, todoist):
    """The same collapse without a mute: a projection that could not run for a
    while (Todoist down) comes back to resolve, reopen, resolve, reopen. One
    occurrence summary and one "back" comment that says it cleared in between,
    not five comments.

    Falsifiable: comment on every turn again and this posts five.
    """
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    task = (await project(db_pool, r.problem_id, now=NOW))["task_id"]
    await _mirror_task(db_pool, task)
    for n in (2, 3, 4, 5):
        at = NOW + timedelta(minutes=n)
        event = _resolved(s, n) if n % 2 == 0 else _occ(s, n, occurred_at=at)
        await ingest_event(db_pool, event, now=at)

    out = await project(db_pool, r.problem_id, now=NOW + timedelta(minutes=10))

    assert out["comments"] == 2
    notes = _notes_on(todoist, task)
    assert notes[0].startswith("⚠️ 2 more occurrences (3 in total)")
    assert notes[1] == (
        "🔁 Back at 2026-09-07 12:05 UTC: recurred inside the reopen window. "
        "It had cleared twice since the last update."
    )
    assert not await _is_completed(db_pool, task)


async def test_a_resolve_before_the_first_sync_still_closes_the_task(db_pool, inbox, todoist):
    """The hub creates its task through the Sync API, and the mirror row only
    appears on the next TodoistSyncFlow tick, up to five minutes later. A
    problem that recovered inside that gap had its close dropped — the close
    refused a task it could not find in the mirror — while the projector
    moved its watermark past the resolve, so nothing ever tried again.

    Falsifiable: refuse a task with no mirror row again and nothing is queued.
    """
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    task = (await project(db_pool, r.problem_id, now=NOW))["task_id"]
    # No `_mirror_task`: the sync has not seen the task yet.
    await ingest_event(db_pool, _resolved(s, 2), now=NOW)
    await project(db_pool, r.problem_id, now=NOW + timedelta(minutes=2))

    assert _notes_on(todoist, task) == ["✅ Resolved at 2026-09-07 12:02 UTC. Closing this task."]
    cmd = await db_pool.fetchval(
        "SELECT command FROM todoist_outbox WHERE temp_id = $1", f"problem-close-{task}"
    )
    assert cmd is not None and cmd["type"] == "item_complete" and cmd["args"]["id"] == task


async def test_a_task_created_through_the_outbox_is_found_once_the_drain_commits_it(
    db_pool, inbox, todoist
):
    """A capture that meets a transient Todoist error goes through the outbox,
    and the problem holds the `item-…` temp id. The drain records the real id
    on the outbox row (`committed_id`) and nowhere else, but the projector
    only looked on the capture idempotency row, which keeps the temp id for
    good. So the problem waited for ever, and its real task was never
    commented on or closed. Prod 92cdd766 (2026-09-11) resolved five minutes
    after its task was created this way, and that task is still open.

    Falsifiable: drop the `committed_id` lookup and the projection still says
    `task_pending_outbox`.
    """
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    todoist["fail_adds"] = True
    temp = (await project(db_pool, r.problem_id, now=NOW))["task_id"]
    todoist["fail_adds"] = False
    assert temp.startswith("item-")
    assert (await project(db_pool, r.problem_id, now=NOW))["skipped"] == "task_pending_outbox"

    # What drain_outbox writes once Todoist accepts the queued item_add.
    real = f"T{uuid.uuid4().hex[:10]}"
    await db_pool.execute(
        "UPDATE todoist_outbox SET status = 'committed', committed_id = $2 WHERE temp_id = $1",
        temp,
        real,
    )
    await _mirror_task(db_pool, real)
    await ingest_event(db_pool, _resolved(s, 2), now=NOW)
    out = await project(db_pool, r.problem_id, now=NOW + timedelta(minutes=5))

    assert out.get("task_id") == real
    assert (await get_problem(db_pool, r.problem_id))["todoist_task_id"] == real
    assert _notes_on(todoist, real) == ["✅ Resolved at 2026-09-07 12:02 UTC. Closing this task."]
    assert await _is_completed(db_pool, real)


async def test_a_problem_that_resolved_before_it_had_a_task_never_gets_one(db_pool, inbox, todoist):
    """A service that failed inside its deploy window and recovered before the
    window ended is `suppressed`, then `resolved`, and never had a task. The
    sweep used to create one anyway — for a problem that was already over —
    and move the watermark past the resolve, so that task never closed.

    Falsifiable: drop the resolved-without-task branch and the sweep creates
    the task.
    """
    s = _subject()
    await set_service_state(db_pool, s, "deploying", minutes=15, set_by="ansible", now=NOW)
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    assert (await get_problem(db_pool, r.problem_id))["status"] == "suppressed"
    await ingest_event(db_pool, _resolved(s, 5), now=NOW + timedelta(minutes=5))
    assert (await get_problem(db_pool, r.problem_id))["status"] == "resolved"

    after = NOW + timedelta(minutes=20)
    mine = [x for x in await project_pending(db_pool, now=after) if x["problem_id"] == r.problem_id]

    assert mine and mine[0].get("skipped") == "resolved_without_task"
    assert not [c for c in _cmds(todoist, "item_add") if c["args"]["content"] == f"Service {s} down"]
    assert (await get_problem(db_pool, r.problem_id))["todoist_task_id"] is None
    assert r.problem_id not in {x["problem_id"] for x in await project_pending(db_pool, now=after)}

    # If it comes back it is a real problem again, and gets its task.
    back = NOW + timedelta(hours=1)
    again = await ingest_event(db_pool, _occ(s, 6, occurred_at=back), now=back)
    assert again.action == "reopened"
    assert (await project(db_pool, r.problem_id, now=back))["created"] is True


# --- the settle window (#537) -------------------------------------------------
# These four turn the window back on; the `inbox` fixture switches it off for
# every other test in this file.


async def _settle(db_pool, value: dict) -> None:
    await db_pool.execute(
        "INSERT INTO settings (key, value) VALUES ('hub_settle_seconds', $1) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        value,
    )


async def test_a_blip_earns_no_task_until_it_outlives_its_window(db_pool, inbox, todoist):
    """A crash-loop that heals in five minutes used to earn a task, get
    clarified and auto-complete — a quarter of the hub's first month of tasks
    were that. An alert now waits out its class's verification window before it
    is believed, and the sweep is what comes back for it.

    Falsifiable: drop the settling branch in `project` and the first call
    creates the task.
    """
    await _settle(db_pool, {})  # code defaults: 300s for DockerServiceDown
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 0, occurred_at=NOW), now=NOW)

    early = await project(db_pool, r.problem_id, now=NOW + timedelta(minutes=2))
    assert early["skipped"] == "settling"
    assert early["settle_seconds"] == 300
    assert (await get_problem(db_pool, r.problem_id))["todoist_task_id"] is None
    assert not _cmds(todoist, "item_add")
    # The sweep still holds it: an open untasked problem is a candidate
    # outright, whatever its watermark.
    waiting = await project_pending(db_pool, now=NOW + timedelta(minutes=2))
    assert r.problem_id in {x["problem_id"] for x in waiting}

    assert (await project(db_pool, r.problem_id, now=NOW + timedelta(minutes=6)))["created"] is True
    # The occurrence it sat on is what describes the task.
    assert f"Heartbeat saw {s}" in _cmds(todoist, "item_add")[0]["args"]["description"]


async def test_a_blip_that_heals_inside_its_window_never_earns_a_task(db_pool, inbox, todoist):
    """The whole point: no task is ever created, not even one born closed."""
    await _settle(db_pool, {})
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 0, occurred_at=NOW), now=NOW)
    early = await project(db_pool, r.problem_id, now=NOW + timedelta(minutes=1))
    assert early["skipped"] == "settling"
    await ingest_event(db_pool, _resolved(s, 4), now=NOW + timedelta(minutes=4))

    after = NOW + timedelta(minutes=30)
    assert (await project(db_pool, r.problem_id, now=after))["skipped"] == "resolved_without_task"
    assert not _cmds(todoist, "item_add")
    assert r.problem_id not in {x["problem_id"] for x in await project_pending(db_pool, now=after)}


async def test_a_recurrence_is_not_held_back(db_pool, inbox, todoist):
    """A blip that comes back is not a blip. `reopen` leaves `first_seen_at`
    alone, so the second episode is already past the window and projects at
    once — deliberately: one 20-second outage is noise, the same one again is
    a pattern, and the first episode's silence is what makes the second worth
    saying. It is also the rule the resolved-without-task branch has always
    stated ("a later occurrence reopens the problem, and THAT projects").
    """
    await _settle(db_pool, {})
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 0, occurred_at=NOW), now=NOW)
    early = await project(db_pool, r.problem_id, now=NOW + timedelta(minutes=1))
    assert early["skipped"] == "settling"
    await ingest_event(db_pool, _resolved(s, 2), now=NOW + timedelta(minutes=2))
    gone = await project(db_pool, r.problem_id, now=NOW + timedelta(minutes=3))
    assert gone["skipped"] == "resolved_without_task"

    back = NOW + timedelta(hours=2)
    again = await ingest_event(db_pool, _occ(s, 10, occurred_at=back), now=back)
    assert again.action == "reopened"
    assert (await project(db_pool, r.problem_id, now=back))["created"] is True


async def test_only_a_signal_that_can_clear_itself_waits(db_pool, inbox, todoist):
    """A money finding is a judgement, not an alert: nothing will send its
    resolution, and no amount of waiting makes it truer. So the window is
    scoped to the producers that clear themselves, and a finding still projects
    on sight.

    Falsifiable: widen `_SELF_CLEARING_SOURCES` to every source and this task
    is three minutes late.
    """
    await _settle(db_pool, {})
    s = _subject()
    r = await ingest_event(
        db_pool,
        Event(
            source="money",
            external_id=f"{s}@stmt",
            kind="occurrence",
            title=f"12 rows on {s} match nothing in the books",
            klass="statement_unmatched",
            subject=s,
            subject_kind="account",
            severity="warning",
            payload={"description": "Reconciliation found rows with no journal entry."},
            occurred_at=NOW,
        ),
        now=NOW,
    )

    assert (await project(db_pool, r.problem_id, now=NOW + timedelta(seconds=1)))["created"] is True
    assert _cmds(todoist, "item_add")[0]["args"]["labels"][0] == MONEY_SOURCE_TAG


async def test_the_settle_window_is_db_configurable(db_pool, inbox, todoist):
    """How long a class takes to prove itself belongs to the operator's homelab,
    so it is a settings row over generic defaults — and a malformed value must
    never stop a problem being handled."""
    await _settle(db_pool, {"dockerservicedown": 0})
    quick = await ingest_event(db_pool, _occ(_subject(), 0, occurred_at=NOW), now=NOW)
    out = await project(db_pool, quick.problem_id, now=NOW + timedelta(seconds=1))
    assert out["created"] is True

    await _settle(db_pool, {"dockerservicedown": "soon"})
    bad = await ingest_event(db_pool, _occ(_subject(), 0, occurred_at=NOW), now=NOW)
    assert (await project(db_pool, bad.problem_id, now=NOW))["settle_seconds"] == 300
