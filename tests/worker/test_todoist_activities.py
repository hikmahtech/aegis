"""Tests for TodoistActivities — projection apply, outbox drain, bootstrap.

Todoist Sync API v1 returns IDs as opaque strings (e.g. '6CrfhM6VCqCcQXPv'),
so every id / project_id / parent_id / committed_id used in these tests is
a Python str, matching the migration-010 schema where those columns are TEXT.
"""

from __future__ import annotations

import pytest
from aegis_worker.activities.todoist import TodoistActivities
from temporalio.testing import ActivityEnvironment


@pytest.mark.asyncio
async def test_apply_sync_diff_upserts_projects_tasks_labels(db_pool):
    """apply_sync_diff is idempotent and writes the three projection tables."""
    activities = TodoistActivities(db_pool=db_pool, connector=None)
    diff = {
        "sync_token": "abc",
        "full_sync": True,
        "projects": [
            {"id": "200", "parent_id": None, "name": "📥 Inbox", "is_archived": False, "child_order": 1},
        ],
        "labels": [
            {"id": "300", "name": "@me", "color": "berry_red"},
        ],
        "items": [
            {
                "id": "100",
                "project_id": "200",
                "parent_id": None,
                "content": "Hello",
                "description": None,
                "due": None,
                "priority": 1,
                "labels": ["@me", "#manual"],
                "checked": False,
                "completed_at": None,
            }
        ],
    }
    env = ActivityEnvironment()
    result = await env.run(activities.apply_sync_diff, diff)
    assert result["projects_upserted"] == 1
    assert result["labels_upserted"] == 1
    assert result["tasks_upserted"] == 1

    async with db_pool.acquire() as conn:
        proj = await conn.fetchrow("SELECT * FROM todoist_projects WHERE id = '200'")
        label = await conn.fetchrow("SELECT * FROM todoist_labels WHERE id = '300'")
        task = await conn.fetchrow("SELECT * FROM todoist_tasks WHERE id = '100'")
    assert proj["name"] == "📥 Inbox"
    assert label["name"] == "@me"
    assert task["content"] == "Hello"
    assert task["assignee_label"] == "@me"
    assert task["source_tag"] == "#manual"

    # Re-apply the same diff: counts unchanged, no duplicates
    result2 = await env.run(activities.apply_sync_diff, diff)
    assert result2["projects_upserted"] == 1
    async with db_pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM todoist_tasks WHERE id = '100'")
    assert count == 1


@pytest.mark.asyncio
async def test_apply_sync_diff_saves_sync_token(db_pool):
    """The new sync_token replaces the stored one for key='main'."""
    activities = TodoistActivities(db_pool=db_pool, connector=None)
    diff = {"sync_token": "next-token", "projects": [], "labels": [], "items": []}
    env = ActivityEnvironment()
    await env.run(activities.apply_sync_diff, diff)
    async with db_pool.acquire() as conn:
        token = await conn.fetchval("SELECT sync_token FROM todoist_sync_state WHERE key = 'main'")
    assert token == "next-token"


@pytest.mark.asyncio
async def test_apply_sync_diff_marks_completed(db_pool):
    """checked=True flips is_completed and stamps completed_at."""
    activities = TodoistActivities(db_pool=db_pool, connector=None)
    # Seed an open task first. Also seed its parent project so the FK is satisfied
    # when this test runs in isolation (project id='200' isn't created by other tests).
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, raw) VALUES ('200', 'Inbox', '{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, is_completed) "
            "VALUES ('101', '200', 'x', false) ON CONFLICT (id) DO UPDATE SET is_completed = false, completed_at = null"
        )
    diff = {
        "sync_token": "t2",
        "projects": [],
        "labels": [],
        "items": [
            {
                "id": "101",
                "project_id": "200",
                "content": "x",
                "labels": [],
                "checked": True,
                "completed_at": "2026-05-18T12:00:00Z",
            }
        ],
    }
    env = ActivityEnvironment()
    await env.run(activities.apply_sync_diff, diff)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT is_completed, completed_at FROM todoist_tasks WHERE id = '101'")
    assert row["is_completed"] is True
    assert row["completed_at"] is not None


@pytest.mark.asyncio
async def test_apply_sync_diff_skips_orphan_item(db_pool):
    """An item whose project_id isn't in this diff or the projection is skipped,
    not allowed to FK-violate the whole transaction.

    Reproduces a real prod incident at 2026-05-18 18:55 UTC: Todoist returned
    an incremental diff containing an item with project_id='2222222222222222'
    but the corresponding project wasn't in the diff. Without the guard,
    apply_sync_diff would crash with ForeignKeyViolationError on every retry,
    blocking the sync_token from advancing.
    """
    activities = TodoistActivities(db_pool=db_pool, connector=None)
    diff = {
        "sync_token": "after-orphan",
        "projects": [],
        "labels": [],
        "items": [
            {
                "id": "orphan-task-1",
                "project_id": "2222222222222222",  # NOT in projection
                "content": "Orphaned task",
                "labels": [],
                "checked": False,
            }
        ],
    }
    # Clear any leftover task with this id from prior tests.
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM todoist_tasks WHERE id = 'orphan-task-1'")

    env = ActivityEnvironment()
    # Must NOT raise — the orphan is logged and skipped, sync_token still advances.
    result = await env.run(activities.apply_sync_diff, diff)
    assert result["tasks_upserted"] == 1  # the count is items-in-diff, not items-actually-written
    async with db_pool.acquire() as conn:
        task = await conn.fetchrow("SELECT id FROM todoist_tasks WHERE id = 'orphan-task-1'")
        token = await conn.fetchval("SELECT sync_token FROM todoist_sync_state WHERE key = 'main'")
    assert task is None, "orphan task must not be inserted"
    assert token == "after-orphan", "sync_token must advance past the orphan diff"


@pytest.mark.asyncio
async def test_drain_outbox_submits_pending_and_marks_committed(db_pool):
    """drain_outbox submits pending rows and marks them committed."""

    class StubConnector:
        async def commands(self, cmds):
            return {
                "ok": True,
                "data": {
                    "sync_status": {c["uuid"]: "ok" for c in cmds},
                    "temp_id_mapping": {c["temp_id"]: str(999 + i) for i, c in enumerate(cmds)},
                },
                "error": None,
                "retryable": False,
                "external_ref": None,
            }

    activities = TodoistActivities(db_pool=db_pool, connector=StubConnector())
    # Seed two pending outbox rows
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM todoist_outbox")
        await conn.execute(
            "INSERT INTO todoist_outbox (temp_id, command, status) VALUES ($1, $2, 'pending'), ($3, $4, 'pending')",
            "temp-a",
            {"type": "item_add", "uuid": "u-a", "temp_id": "temp-a", "args": {}},
            "temp-b",
            {"type": "item_add", "uuid": "u-b", "temp_id": "temp-b", "args": {}},
        )

    env = ActivityEnvironment()
    result = await env.run(activities.drain_outbox)
    assert result["committed"] == 2
    assert result["failed"] == 0

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT temp_id, status, committed_id FROM todoist_outbox"
        )
    assert {r["status"] for r in rows} == {"committed"}
    by_temp = {r["temp_id"]: r for r in rows}
    assert "temp-a" in by_temp and by_temp["temp-a"]["committed_id"] in ("999", "1000")
    assert "temp-b" in by_temp and by_temp["temp-b"]["committed_id"] in ("999", "1000")
    assert by_temp["temp-a"]["committed_id"] != by_temp["temp-b"]["committed_id"]


@pytest.mark.asyncio
async def test_drain_outbox_retries_on_retryable_error(db_pool):
    """A retryable failure increments attempt_count and leaves status='pending'."""

    class FailingConnector:
        async def commands(self, cmds):
            return {"ok": False, "data": None, "error": "http_503", "retryable": True}

    activities = TodoistActivities(db_pool=db_pool, connector=FailingConnector())
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM todoist_outbox")
        await conn.execute(
            "INSERT INTO todoist_outbox (temp_id, command, status, attempt_count) VALUES ($1, $2, 'pending', 0)",
            "temp-c",
            {"type": "item_add", "uuid": "u-c", "temp_id": "temp-c", "args": {}},
        )

    env = ActivityEnvironment()
    result = await env.run(activities.drain_outbox)
    assert result["committed"] == 0
    assert result["failed"] == 0  # retryable, not terminal

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status, attempt_count FROM todoist_outbox WHERE temp_id = 'temp-c'")
    assert row["status"] == "pending"
    assert row["attempt_count"] == 1


@pytest.mark.asyncio
async def test_drain_outbox_marks_failed_after_max_attempts(db_pool):
    """Non-retryable error OR attempt_count >= 5 marks the row failed."""

    class HardFailConnector:
        async def commands(self, cmds):
            return {"ok": False, "data": None, "error": "http_400", "retryable": False}

    activities = TodoistActivities(db_pool=db_pool, connector=HardFailConnector())
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM todoist_outbox")
        await conn.execute(
            "INSERT INTO todoist_outbox (temp_id, command, status, attempt_count) VALUES ($1, $2, 'pending', 0)",
            "temp-d",
            {"type": "item_add", "uuid": "u-d", "temp_id": "temp-d", "args": {}},
        )

    env = ActivityEnvironment()
    result = await env.run(activities.drain_outbox)
    assert result["failed"] == 1

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status FROM todoist_outbox WHERE temp_id = 'temp-d'")
    assert row["status"] == "failed"


@pytest.mark.asyncio
async def test_bootstrap_creates_managed_projects_when_account_empty(db_pool):
    """If the projection has no managed projects, bootstrap creates them."""

    created: list[dict] = []

    class StubConnector:
        async def sync(self, token, resource_types):
            # Empty-ish account: only the default Todoist Inbox exists.
            return {
                "ok": True,
                "data": {
                    "sync_token": "boot",
                    "projects": [
                        {"id": "inbox-default-id", "name": "Inbox", "parent_id": None, "inbox_project": True, "is_archived": False},
                    ],
                    "items": [],
                    "labels": [],
                    "full_sync": True,
                },
                "error": None,
                "retryable": False,
            }

        async def commands(self, cmds):
            created.extend(cmds)
            mapping = {c["temp_id"]: str(1000 + i) for i, c in enumerate(cmds)}
            return {
                "ok": True,
                "data": {
                    "sync_status": {c["uuid"]: "ok" for c in cmds},
                    "temp_id_mapping": mapping,
                },
                "error": None,
                "retryable": False,
            }

    activities = TodoistActivities(db_pool=db_pool, connector=StubConnector())
    # Settings row absent → bootstrap should run. Cascade-delete dependent
    # rows first so sibling tests' synthetic todoist_tasks rows don't
    # FK-violate the DELETE FROM todoist_projects below.
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM settings WHERE key = 'todoist_managed_project_ids'")
        await conn.execute(
            "DELETE FROM todoist_notes WHERE item_id IN "
            "(SELECT id FROM todoist_tasks WHERE project_id IN "
            "(SELECT id FROM todoist_projects WHERE is_managed = true))"
        )
        await conn.execute(
            "DELETE FROM todoist_tasks WHERE project_id IN "
            "(SELECT id FROM todoist_projects WHERE is_managed = true)"
        )
        # Self-ref FK: NULL out parent_id pointing at to-be-deleted projects
        # so the next DELETE doesn't violate todoist_projects_parent_id_fkey.
        await conn.execute(
            "UPDATE todoist_projects SET parent_id = NULL "
            "WHERE parent_id IN (SELECT id FROM todoist_projects WHERE is_managed = true)"
        )
        await conn.execute("DELETE FROM todoist_projects WHERE is_managed = true")

    env = ActivityEnvironment()
    result = await env.run(activities.bootstrap_if_empty)
    assert result["bootstrapped"] is True
    # Todoist restructure (2026-07): Next/Someday are @next / @someday
    # labels now, not managed projects — the seed's managed_projects list
    # is empty, so bootstrap creates nothing via the API; inbox is adopted.
    assert result["projects_created"] == 0
    assert result["inbox_adopted_id"] == "inbox-default-id"

    # Settings row has only the adopted inbox key
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key = 'todoist_managed_project_ids'")
    assert row is not None
    assert set(row["value"].keys()) == {"inbox"}
    assert row["value"]["inbox"] == "inbox-default-id"


@pytest.mark.asyncio
async def test_bootstrap_skips_when_settings_already_populated(db_pool):
    """A populated settings row → bootstrap is a no-op."""

    class TrackedConnector:
        called = False

        async def sync(self, *a, **k):
            return {"ok": True, "data": {"sync_token": "x", "projects": [], "items": [], "labels": []}, "error": None, "retryable": False}

        async def commands(self, cmds):
            self.called = True
            return {"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}, "error": None, "retryable": False}

    activities = TodoistActivities(db_pool=db_pool, connector=TrackedConnector())
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('todoist_managed_project_ids', $1) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"inbox": "1", "next": "2", "someday": "3"},
        )

    env = ActivityEnvironment()
    result = await env.run(activities.bootstrap_if_empty)
    assert result["bootstrapped"] is False
    assert activities.connector.called is False  # no commands sent


@pytest.mark.asyncio
async def test_bootstrap_proceeds_alongside_user_projects(db_pool):
    """Bootstrap creates managed projects alongside any existing user-owned
    projects. The earlier 'account_not_empty' refusal was relaxed at deploy
    time because the user has a populated Todoist account.
    """

    captured_cmds: list[dict] = []

    class NonEmptyConnector:
        async def sync(self, token, resource_types):
            return {
                "ok": True,
                "data": {
                    "sync_token": "x",
                    "projects": [
                        {"id": "inbox-id", "name": "Inbox", "parent_id": None, "inbox_project": True, "is_archived": False},
                        {"id": "999", "name": "User project", "parent_id": None, "inbox_project": False, "is_archived": False},
                    ],
                    "items": [],
                    "labels": [],
                    "full_sync": True,
                },
                "error": None,
                "retryable": False,
            }

        async def commands(self, cmds):
            captured_cmds.extend(cmds)
            return {
                "ok": True,
                "data": {
                    "sync_status": {c["uuid"]: "ok" for c in cmds},
                    "temp_id_mapping": {c["temp_id"]: f"new-{i}" for i, c in enumerate(cmds)},
                },
                "error": None,
                "retryable": False,
            }

    activities = TodoistActivities(db_pool=db_pool, connector=NonEmptyConnector())
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM settings WHERE key = 'todoist_managed_project_ids'")
        await conn.execute(
            "DELETE FROM todoist_notes WHERE item_id IN "
            "(SELECT id FROM todoist_tasks WHERE project_id IN "
            "(SELECT id FROM todoist_projects WHERE is_managed = true))"
        )
        await conn.execute(
            "DELETE FROM todoist_tasks WHERE project_id IN "
            "(SELECT id FROM todoist_projects WHERE is_managed = true)"
        )
        # Self-ref FK: NULL out parent_id pointing at to-be-deleted projects
        # so the next DELETE doesn't violate todoist_projects_parent_id_fkey.
        await conn.execute(
            "UPDATE todoist_projects SET parent_id = NULL "
            "WHERE parent_id IN (SELECT id FROM todoist_projects WHERE is_managed = true)"
        )
        await conn.execute("DELETE FROM todoist_projects WHERE is_managed = true")

    env = ActivityEnvironment()
    result = await env.run(activities.bootstrap_if_empty)
    assert result["bootstrapped"] is True
    # Todoist restructure (2026-07): no managed projects left in the seed
    # to create — Next/Someday are labels now.
    assert result["projects_created"] == 0
    assert result["inbox_adopted_id"] == "inbox-id"
    # The user's 'User project' is NOT in the managed-ids map.
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key = 'todoist_managed_project_ids'")
    assert "999" not in row["value"].values(), "Bootstrap should not adopt user's project as managed"
    assert row["value"]["inbox"] == "inbox-id"


@pytest.mark.asyncio
async def test_bootstrap_recovers_from_partial_failure(db_pool):
    """If projection has is_managed rows from a prior bootstrap but settings
    row was lost, the activity recovers settings from the projection without
    making any API calls."""

    class NoOpConnector:
        async def sync(self, *a, **k):
            raise AssertionError("sync must not be called during recovery")

        async def commands(self, cmds):
            raise AssertionError("commands must not be called during recovery")

    activities = TodoistActivities(db_pool=db_pool, connector=NoOpConnector())
    # Clean state: no settings row, but 1 managed projection row — the
    # adopted default Inbox (name not in the seed; recovery falls back to
    # "the sole row whose name isn't in managed_projects is the inbox").
    # Todoist restructure (2026-07): the seed's managed_projects list is
    # now empty (Next/Someday are @next / @someday labels, not projects),
    # so a partial bootstrap can only ever have left behind the adopted
    # Inbox row.
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM settings WHERE key = 'todoist_managed_project_ids'")
        await conn.execute(
            "DELETE FROM todoist_notes WHERE item_id IN "
            "(SELECT id FROM todoist_tasks WHERE project_id IN "
            "(SELECT id FROM todoist_projects WHERE is_managed = true))"
        )
        await conn.execute(
            "DELETE FROM todoist_tasks WHERE project_id IN "
            "(SELECT id FROM todoist_projects WHERE is_managed = true)"
        )
        # Self-ref FK: NULL out parent_id pointing at to-be-deleted projects
        # so the next DELETE doesn't violate todoist_projects_parent_id_fkey.
        await conn.execute(
            "UPDATE todoist_projects SET parent_id = NULL "
            "WHERE parent_id IN (SELECT id FROM todoist_projects WHERE is_managed = true)"
        )
        await conn.execute("DELETE FROM todoist_projects WHERE is_managed = true")
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ($1, $2, true, '{}'::jsonb) ON CONFLICT (id) DO UPDATE "
            "SET name = EXCLUDED.name, is_managed = true",
            "5001",
            "Inbox",  # the adopted default Inbox
        )

    env = ActivityEnvironment()
    result = await env.run(activities.bootstrap_if_empty)
    assert result["bootstrapped"] is False
    assert result["reason"] == "recovered_from_projection"

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key = 'todoist_managed_project_ids'")
    assert row is not None
    assert row["value"] == {"inbox": "5001"}


@pytest.mark.asyncio
async def test_fetch_sync_requests_notes_resource(db_pool) -> None:
    """fetch_sync includes 'notes' in resource_types (Phase 3 comment loop)."""
    from unittest.mock import AsyncMock

    connector = AsyncMock()
    connector.sync = AsyncMock(
        return_value={
            "ok": True,
            "data": {"sync_token": "abc", "items": [], "projects": [], "labels": [], "notes": []},
        }
    )
    acts = TodoistActivities(db_pool=db_pool, connector=connector)
    await acts.fetch_sync()
    # Connector called with resource_types including 'notes'
    call_kwargs = connector.sync.await_args.kwargs
    if call_kwargs:
        resource_types = call_kwargs["resource_types"]
    else:
        # positional call: sync(token, resource_types)
        resource_types = connector.sync.await_args.args[1]
    assert "notes" in resource_types, resource_types
    assert "items" in resource_types
    assert "projects" in resource_types
    assert "labels" in resource_types


@pytest.mark.asyncio
async def test_apply_sync_diff_skips_deleted_labels_with_empty_name(db_pool):
    """Todoist's sync diff re-sends is_deleted=true labels with empty
    names. Two such labels in the same diff used to collide on
    UNIQUE(name) — INSERT(name='') succeeded for the first, INSERT for
    the second failed with UniqueViolationError, and the whole
    apply_sync_diff aborted (so tasks/projects also never landed).
    Fix: skip is_deleted/empty-name labels and delete any pre-existing
    rows that flipped to is_deleted."""
    activities = TodoistActivities(db_pool=db_pool, connector=None)

    # Seed a row that exists locally but is now is_deleted in Todoist.
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM todoist_labels WHERE id IN ('LBL_DEL_1','LBL_DEL_2','LBL_LIVE')"
        )
        await conn.execute(
            "INSERT INTO todoist_labels (id, name, color, raw) "
            "VALUES ('LBL_DEL_1','@old-name','charcoal','{}'::jsonb)"
        )

    diff = {
        "sync_token": "tok",
        "projects": [],
        "labels": [
            # Two is_deleted labels with empty names — old failure case.
            {"id": "LBL_DEL_1", "name": "", "color": "charcoal", "is_deleted": True},
            {"id": "LBL_DEL_2", "name": "", "color": "berry_red", "is_deleted": True},
            # One live label landing fine.
            {"id": "LBL_LIVE", "name": "@new", "color": "lavender", "is_deleted": False},
        ],
        "items": [],
    }
    env = ActivityEnvironment()
    # Must not raise.
    await env.run(activities.apply_sync_diff, diff)

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name FROM todoist_labels WHERE id IN "
            "('LBL_DEL_1','LBL_DEL_2','LBL_LIVE') ORDER BY id"
        )
    ids = {r["id"]: r["name"] for r in rows}
    # is_deleted labels removed (even the pre-existing one we'd seeded)
    assert "LBL_DEL_1" not in ids
    assert "LBL_DEL_2" not in ids
    # Live label landed
    assert ids.get("LBL_LIVE") == "@new"


@pytest.mark.asyncio
async def test_apply_sync_diff_agent_reply_note_does_not_bump_last_note_at(db_pool):
    """Pin (2026-05-28): `[Agent reply @ ...]` notes are AEGIS-authored
    (posted by AgentChatReplyFlow's per-agent comment-channel reply
    path) and MUST NOT bump `todoist_tasks.last_note_at`. Without this
    filter, the agent's own reply re-surfaces the task in
    find_unclassified_items via a different code path than the user-
    note `MAX(posted_at)` filter — defence-in-depth for the loop fix
    in PR worktree-workflow-leanness-sweep.
    """
    activities = TodoistActivities(db_pool=db_pool, connector=None)
    # Seed a task with last_note_at NULL (the eligibility-test state).
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('PRJ_AR','p',true,'{}'::jsonb) ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute("DELETE FROM todoist_notes WHERE item_id = 'TASK_AR'")
        await conn.execute("DELETE FROM todoist_tasks WHERE id = 'TASK_AR'")
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, raw) "
            "VALUES ('TASK_AR','PRJ_AR','t',ARRAY['@raphael'],'{}'::jsonb)"
        )

    diff = {
        "sync_token": "agent-reply-tok",
        "projects": [],
        "labels": [],
        "items": [],
        "notes": [
            {
                "id": "N_AGENT",
                "item_id": "TASK_AR",
                "content": (
                    "[Agent reply @ 12:00 UTC agent=raphael]\n"
                    "Here's what I think about your question."
                ),
                "posted_at": "2026-05-28T12:00:00Z",
                "posted_uid": "AEGIS",
            },
        ],
    }
    env = ActivityEnvironment()
    await env.run(activities.apply_sync_diff, diff)

    async with db_pool.acquire() as conn:
        last_note_at = await conn.fetchval(
            "SELECT last_note_at FROM todoist_tasks WHERE id = 'TASK_AR'"
        )
        note_row = await conn.fetchrow(
            "SELECT content FROM todoist_notes WHERE id = 'N_AGENT'"
        )

    # Note is recorded in projection (we still want to mirror what Todoist
    # sees) but last_note_at remains NULL — the agent's own reply doesn't
    # re-eligible the task in find_unclassified_items.
    assert note_row is not None
    assert "Agent reply" in note_row["content"]
    assert last_note_at is None


@pytest.mark.asyncio
async def test_apply_sync_diff_user_note_still_bumps_last_note_at(db_pool):
    """Companion to the agent-reply test above: a genuine user note
    DOES bump last_note_at, so the filter discriminates correctly.
    """
    import datetime as _dt

    activities = TodoistActivities(db_pool=db_pool, connector=None)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('PRJ_UR','p',true,'{}'::jsonb) ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute("DELETE FROM todoist_notes WHERE item_id = 'TASK_UR'")
        await conn.execute("DELETE FROM todoist_tasks WHERE id = 'TASK_UR'")
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, raw) "
            "VALUES ('TASK_UR','PRJ_UR','t',ARRAY['@raphael'],'{}'::jsonb)"
        )

    diff = {
        "sync_token": "user-note-tok",
        "projects": [],
        "labels": [],
        "items": [],
        "notes": [
            {
                "id": "N_USER",
                "item_id": "TASK_UR",
                "content": "What about the bcp double-suffix bug?",
                "posted_at": "2026-05-28T12:00:00Z",
                "posted_uid": "USER",
            },
        ],
    }
    env = ActivityEnvironment()
    await env.run(activities.apply_sync_diff, diff)

    async with db_pool.acquire() as conn:
        last_note_at = await conn.fetchval(
            "SELECT last_note_at FROM todoist_tasks WHERE id = 'TASK_UR'"
        )
    assert last_note_at is not None
    assert last_note_at == _dt.datetime(2026, 5, 28, 12, 0, tzinfo=_dt.UTC)
