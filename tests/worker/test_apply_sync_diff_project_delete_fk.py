"""apply_sync_diff must survive project deletions without wedging the sync.

Two prod incidents, same shape — `todoist_tasks_project_id_fkey` raised inside
the single transaction that also advances `todoist_sync_state.sync_token`, so
the token never moved and Todoist re-served the identical poisoned diff every
5 minutes until someone did manual DB surgery.

2026-05-25: project + its tasks both deleted in the SAME diff, but project
DELETEs ran before item DELETEs. Fixed by deferring project DELETEs to the end.

2026-08-02: project `6h2fmwpmjgrh8m4x` ("Security Service") deleted with 48
local task rows still referencing it. Todoist's incremental sync marks only the
PROJECT `is_deleted` — it emits no per-item deletes for the items that lived
inside it, so the items pass had nothing to delete and the 2026-05-25 deferral
did not help. Fixed by cascading (notes → tasks → project) under the projects
we are actually deleting, and by running every DELETE in a SAVEPOINT so a
residual FK hazard degrades to a stale row instead of jamming the token.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis_worker.activities.todoist import TodoistActivities

# Every row this module seeds is prefixed so cleanup can never touch a
# neighbouring test's data (the worker suite shares one DB per xdist worker).
PFX = "PDFK_"


async def _wipe(conn) -> None:
    """Delete this module's rows children-before-parents."""
    await conn.execute(f"DELETE FROM todoist_notes WHERE id LIKE '{PFX}%'")
    # parent_id is a self-FK: clear it before deleting so order can't bite.
    await conn.execute(f"UPDATE todoist_tasks SET parent_id = NULL WHERE id LIKE '{PFX}%'")
    await conn.execute(f"DELETE FROM todoist_tasks WHERE id LIKE '{PFX}%'")
    await conn.execute(f"DELETE FROM todoist_projects WHERE id LIKE '{PFX}%'")


@pytest_asyncio.fixture(loop_scope="function")
async def clean(db_pool):
    """Clean slate before and after, plus save/restore the singleton
    `todoist_sync_state` row these tests assert on."""
    async with db_pool.acquire() as conn:
        await _wipe(conn)
        original_token = await conn.fetchval(
            "SELECT sync_token FROM todoist_sync_state WHERE key = 'main'"
        )
    yield db_pool
    async with db_pool.acquire() as conn:
        await _wipe(conn)
        await conn.execute(
            "UPDATE todoist_sync_state SET sync_token = $1 WHERE key = 'main'",
            original_token,
        )


async def _set_token(db_pool, token: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE todoist_sync_state SET sync_token = $1 WHERE key = 'main'", token
        )


async def _token(db_pool) -> str:
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT sync_token FROM todoist_sync_state WHERE key = 'main'")


def _acts(db_pool) -> TodoistActivities:
    return TodoistActivities(db_pool=db_pool, connector=AsyncMock())


# ---------------------------------------------------------------------------
# 2026-05-25 regression: project + tasks deleted in the same diff.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_delete_with_referencing_task_in_same_diff(clean, db_pool) -> None:
    """Diff carrying `project.is_deleted` AND `task.is_deleted` for a task that
    references it must apply cleanly (project DELETEs deferred past item ones)."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            f"VALUES ('{PFX}P', 'doomed', false, '{{}}'::jsonb)"
        )
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, raw) "
            f"VALUES ('{PFX}T', '{PFX}P', 'doomed task', '{{}}'::text[], '{{}}'::jsonb)"
        )

    await _acts(db_pool).apply_sync_diff(
        {
            "projects": [{"id": f"{PFX}P", "is_deleted": True}],
            "labels": [],
            "items": [{"id": f"{PFX}T", "is_deleted": True}],
            "notes": [],
        }
    )

    async with db_pool.acquire() as conn:
        assert await conn.fetchval(
            f"SELECT count(*) FROM todoist_projects WHERE id = '{PFX}P'"
        ) == 0
        assert await conn.fetchval(f"SELECT count(*) FROM todoist_tasks WHERE id = '{PFX}T'") == 0


# ---------------------------------------------------------------------------
# 2026-08-02 regression: the exact prod failure.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_delete_cascades_to_local_tasks_absent_from_diff(clean, db_pool) -> None:
    """THE prod repro. Project arrives `is_deleted`; its tasks are NOT in the
    diff at all (Todoist never sends them). Pre-fix this raised
    ForeignKeyViolationError on `todoist_tasks_project_id_fkey`, rolled back the
    whole transaction and froze the sync_token — wedged, every 5 minutes.

    Post-fix the tasks are cascade-deleted, the project goes, and — the
    assertion that actually encodes "not wedged" — the token advances.
    """
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            f"VALUES ('{PFX}DOOM', 'Security Service', false, '{{}}'::jsonb)"
        )
        # A survivor project + task, to prove the cascade is scoped.
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            f"VALUES ('{PFX}KEEP', 'untouched', false, '{{}}'::jsonb)"
        )
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, raw) "
            f"VALUES ('{PFX}SAFE', '{PFX}KEEP', 'unrelated', '{{}}'::text[], '{{}}'::jsonb)"
        )
        for i in range(3):
            await conn.execute(
                "INSERT INTO todoist_tasks (id, project_id, content, labels, raw) "
                f"VALUES ('{PFX}D{i}', '{PFX}DOOM', 'doomed {i}', "
                "'{}'::text[], '{}'::jsonb)"
            )
        # ...one of them a subtask of another, so the DELETE also has to satisfy
        # the todoist_tasks_parent_id_fkey self-reference within the same set.
        await conn.execute(
            f"UPDATE todoist_tasks SET parent_id = '{PFX}D0' WHERE id = '{PFX}D1'"
        )

    await _set_token(db_pool, "token-before-delete")

    result = await _acts(db_pool).apply_sync_diff(
        {
            "projects": [{"id": f"{PFX}DOOM", "is_deleted": True}],
            "labels": [],
            "items": [],  # Todoist sends NO item deletes for a deleted project
            "notes": [],
            "sync_token": "token-after-delete",
        }
    )

    async with db_pool.acquire() as conn:
        assert await conn.fetchval(
            f"SELECT count(*) FROM todoist_projects WHERE id = '{PFX}DOOM'"
        ) == 0, "deleted project should be gone"
        assert await conn.fetchval(
            f"SELECT count(*) FROM todoist_tasks WHERE project_id = '{PFX}DOOM'"
        ) == 0, "its tasks should have been cascade-deleted"
        # Scoped: the unrelated project and its task are untouched.
        assert await conn.fetchval(
            f"SELECT count(*) FROM todoist_projects WHERE id = '{PFX}KEEP'"
        ) == 1
        assert await conn.fetchval(f"SELECT count(*) FROM todoist_tasks WHERE id = '{PFX}SAFE'") == 1

    assert await _token(db_pool) == "token-after-delete", (
        "sync_token must advance — a frozen token IS the wedge"
    )
    assert result["degraded_deletes"] == [], "the happy path must not degrade anything"


@pytest.mark.asyncio
async def test_project_delete_cascade_removes_notes_on_those_tasks(clean, db_pool) -> None:
    """todoist_notes.item_id → todoist_tasks(id) has no ON DELETE CASCADE, so a
    cascade that deletes tasks without clearing their notes first just swaps
    todoist_tasks_project_id_fkey for todoist_notes_item_id_fkey — same wedge,
    different constraint name."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            f"VALUES ('{PFX}NP', 'has commented tasks', false, '{{}}'::jsonb)"
        )
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, raw) "
            f"VALUES ('{PFX}NT', '{PFX}NP', 'commented', '{{}}'::text[], '{{}}'::jsonb)"
        )
        await conn.execute(
            "INSERT INTO todoist_notes (id, item_id, content, posted_at, raw) "
            f"VALUES ('{PFX}N1', '{PFX}NT', 'a comment', now(), '{{}}'::jsonb)"
        )
        # Guard against an "empty result is empty because nothing was seeded"
        # pass: prove the note is really there before we start.
        assert await conn.fetchval(f"SELECT count(*) FROM todoist_notes WHERE id = '{PFX}N1'") == 1

    await _set_token(db_pool, "before-notes")

    result = await _acts(db_pool).apply_sync_diff(
        {
            "projects": [{"id": f"{PFX}NP", "is_deleted": True}],
            "labels": [],
            "items": [],
            "notes": [],
            "sync_token": "after-notes",
        }
    )

    async with db_pool.acquire() as conn:
        assert await conn.fetchval(f"SELECT count(*) FROM todoist_notes WHERE id = '{PFX}N1'") == 0
        assert await conn.fetchval(f"SELECT count(*) FROM todoist_tasks WHERE id = '{PFX}NT'") == 0
        assert await conn.fetchval(
            f"SELECT count(*) FROM todoist_projects WHERE id = '{PFX}NP'"
        ) == 0
    assert await _token(db_pool) == "after-notes"
    assert result["degraded_deletes"] == []


@pytest.mark.asyncio
async def test_managed_project_delete_preserves_project_and_its_tasks(clean, db_pool) -> None:
    """Managed projects are deliberately kept even when Todoist says they're
    gone. The cascade must be scoped to the projects we ACTUALLY delete —
    otherwise it would strand a managed project's rows by deleting its tasks
    while keeping the project row."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            f"VALUES ('{PFX}MP', 'inbox-ish', true, '{{}}'::jsonb)"
        )
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, raw) "
            f"VALUES ('{PFX}MT', '{PFX}MP', 'keep me', '{{}}'::text[], '{{}}'::jsonb)"
        )

    result = await _acts(db_pool).apply_sync_diff(
        {
            "projects": [{"id": f"{PFX}MP", "is_deleted": True}],
            "labels": [],
            "items": [],
            "notes": [],
            "sync_token": "managed-kept",
        }
    )

    async with db_pool.acquire() as conn:
        assert await conn.fetchval(
            f"SELECT count(*) FROM todoist_projects WHERE id = '{PFX}MP'"
        ) == 1, "managed project must be preserved"
        assert await conn.fetchval(
            f"SELECT count(*) FROM todoist_tasks WHERE id = '{PFX}MT'"
        ) == 1, "a preserved project must keep its tasks"
    assert result["degraded_deletes"] == []


@pytest.mark.asyncio
async def test_project_delete_reparents_surviving_child_in_another_project(
    clean, db_pool
) -> None:
    """A surviving task elsewhere that hangs off a doomed task is re-parented to
    top level, not dropped — same policy as the items pass's orphan-parent
    guard, and it keeps todoist_tasks_parent_id_fkey satisfied."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            f"VALUES ('{PFX}RD', 'doomed', false, '{{}}'::jsonb)"
        )
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            f"VALUES ('{PFX}RS', 'survivor', false, '{{}}'::jsonb)"
        )
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, raw) "
            f"VALUES ('{PFX}RDT', '{PFX}RD', 'doomed parent', '{{}}'::text[], '{{}}'::jsonb)"
        )
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, parent_id, content, labels, raw) "
            f"VALUES ('{PFX}RST', '{PFX}RS', '{PFX}RDT', 'orphaned child', "
            "'{}'::text[], '{}'::jsonb)"
        )

    result = await _acts(db_pool).apply_sync_diff(
        {
            "projects": [{"id": f"{PFX}RD", "is_deleted": True}],
            "labels": [],
            "items": [],
            "notes": [],
            "sync_token": "reparented",
        }
    )

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT project_id, parent_id FROM todoist_tasks WHERE id = '{PFX}RST'"
        )
        assert row is not None, "the surviving child must NOT be deleted"
        assert row["project_id"] == f"{PFX}RS"
        assert row["parent_id"] is None, "its dangling parent_id must be nulled"
        assert await conn.fetchval(f"SELECT count(*) FROM todoist_tasks WHERE id = '{PFX}RDT'") == 0
    assert result["degraded_deletes"] == []


# ---------------------------------------------------------------------------
# Degradation: an FK the cascade does NOT fix must not take the sync down.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failing_item_delete_degrades_instead_of_wedging_the_token(clean, db_pool) -> None:
    """No project deletion here, so the cascade above cannot help — this can
    only pass because each DELETE runs in its own SAVEPOINT.

    Shape: Todoist reports a parent task deleted, but its subtask exists only in
    our projection (not in the diff), so `DELETE FROM todoist_tasks WHERE id =
    ANY(...)` violates todoist_tasks_parent_id_fkey. Pre-fix that aborted the
    entire transaction and froze the token. Post-fix the `items` phase rolls
    back alone: the stale rows stay, everything else in the diff still commits,
    the token advances, and the divergence is reported.
    """
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            f"VALUES ('{PFX}GP', 'alive', false, '{{}}'::jsonb)"
        )
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, raw) "
            f"VALUES ('{PFX}GPARENT', '{PFX}GP', 'parent', '{{}}'::text[], '{{}}'::jsonb)"
        )
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, parent_id, content, labels, raw) "
            f"VALUES ('{PFX}GCHILD', '{PFX}GP', '{PFX}GPARENT', 'child not in diff', "
            "'{}'::text[], '{}'::jsonb)"
        )

    await _set_token(db_pool, "before-degrade")

    result = await _acts(db_pool).apply_sync_diff(
        {
            "projects": [],
            "labels": [],
            "items": [
                {"id": f"{PFX}GPARENT", "is_deleted": True},
                # A normal upsert in the same diff — proves the transaction
                # actually COMMITTED rather than merely not raising.
                {
                    "id": f"{PFX}GNEW",
                    "project_id": f"{PFX}GP",
                    "content": "arrived in the poisoned diff",
                    "labels": [],
                },
            ],
            "notes": [],
            "sync_token": "after-degrade",
        }
    )

    assert "items" in result["degraded_deletes"], (
        f"the failing DELETE must be reported, got {result['degraded_deletes']!r}"
    )
    assert await _token(db_pool) == "after-degrade", "token must advance despite the failed delete"

    async with db_pool.acquire() as conn:
        assert await conn.fetchval(
            f"SELECT count(*) FROM todoist_tasks WHERE id = '{PFX}GNEW'"
        ) == 1, "the rest of the diff must still be committed"
        # The undeletable rows are kept (stale, but visible and self-correcting
        # on the next full sync) rather than costing us the whole sync.
        assert await conn.fetchval(
            f"SELECT count(*) FROM todoist_tasks WHERE id = '{PFX}GPARENT'"
        ) == 1
        assert await conn.fetchval(
            f"SELECT count(*) FROM todoist_tasks WHERE id = '{PFX}GCHILD'"
        ) == 1
