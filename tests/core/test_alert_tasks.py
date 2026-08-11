"""Issue #279 — an alert task must close when its alert resolves.

Six tasks outlived their incidents by up to 9 days because the resolve signal
only ever re-armed dedup. These lock in the close and, just as importantly, the
two cases that must NOT close.
"""

from __future__ import annotations

import pytest
from aegis.services.alert_tasks import close_task_for_resolved_alert

FP = "aegis-heartbeat:DockerServiceDown:test_redis"


async def _seed(conn, task_id: str, *, assignee: str = "@pandora", completed: bool = False) -> None:
    await conn.execute(
        "INSERT INTO todoist_projects (id, name, is_managed, raw) "
        "VALUES ('P_INBOX','Inbox',true,'{}'::jsonb) ON CONFLICT (id) DO NOTHING"
    )
    await conn.execute("DELETE FROM todoist_capture_idempotency WHERE external_id = $1", f"alert-{FP}")
    await conn.execute("DELETE FROM todoist_outbox WHERE temp_id = $1", f"alert-resolved-close-{task_id}")
    await conn.execute("DELETE FROM todoist_tasks WHERE id = $1", task_id)
    await conn.execute(
        "INSERT INTO todoist_tasks "
        "(id, project_id, content, labels, assignee_label, source_tag, is_completed, raw) "
        "VALUES ($1,'P_INBOX','Service test_redis down',ARRAY['#alert','@pandora','@waiting'],"
        "        $2,'#alert',$3,'{}'::jsonb)",
        task_id,
        assignee,
        completed,
    )
    await conn.execute(
        "INSERT INTO todoist_capture_idempotency (source_tag, external_id, todoist_task_ref) "
        "VALUES ('#alert', $1, $2)",
        f"alert-{FP}",
        task_id,
    )


@pytest.mark.asyncio
async def test_resolved_alert_closes_its_task(db_pool) -> None:
    async with db_pool.acquire() as conn:
        await _seed(conn, "T_AL_1")

    assert await close_task_for_resolved_alert(db_pool, FP) == "T_AL_1"

    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT is_completed FROM todoist_tasks WHERE id='T_AL_1'")
        cmd = await conn.fetchval(
            "SELECT command FROM todoist_outbox WHERE temp_id='alert-resolved-close-T_AL_1'"
        )
    assert cmd["type"] == "item_complete", cmd
    assert cmd["args"]["id"] == "T_AL_1"


@pytest.mark.asyncio
async def test_close_is_idempotent_across_flapping(db_pool) -> None:
    """A flapping service writes many resolved rows (prod: 14 for one service).
    Re-closing must not error, and must not spawn duplicate outbox rows."""
    async with db_pool.acquire() as conn:
        await _seed(conn, "T_AL_2")

    assert await close_task_for_resolved_alert(db_pool, FP) == "T_AL_2"
    # Second resolve: task is already completed, so there is nothing to close.
    assert await close_task_for_resolved_alert(db_pool, FP) is None

    async with db_pool.acquire() as conn:
        rows = await conn.fetchval(
            "SELECT count(*) FROM todoist_outbox WHERE temp_id='alert-resolved-close-T_AL_2'"
        )
    assert rows == 1, "deterministic temp_id must not fan out on flap"


@pytest.mark.asyncio
async def test_task_the_user_claimed_is_left_alone(db_pool) -> None:
    """Once the user takes the task (@me), closing it out from under them is
    worse than leaving it stale."""
    async with db_pool.acquire() as conn:
        await _seed(conn, "T_AL_3", assignee="@me")

    assert await close_task_for_resolved_alert(db_pool, FP) is None

    async with db_pool.acquire() as conn:
        assert not await conn.fetchval(
            "SELECT is_completed FROM todoist_tasks WHERE id='T_AL_3'"
        )


@pytest.mark.asyncio
async def test_unknown_fingerprint_and_no_pool_are_quiet(db_pool) -> None:
    assert await close_task_for_resolved_alert(db_pool, "no-such-fingerprint") is None
    assert await close_task_for_resolved_alert(db_pool, "") is None
    assert await close_task_for_resolved_alert(None, FP) is None
