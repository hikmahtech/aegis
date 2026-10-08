"""Terminal-state writers. Every non-complete exit MUST park at @waiting —
otherwise the 6h cooldown becomes an infinite slow loop over the same tasks."""

from __future__ import annotations

import pytest_asyncio
from aegis_worker.activities.agent_task import PARK_LABEL, AgentTaskActivities


@pytest_asyncio.fixture(loop_scope="function")
async def _seed(db_pool):
    await db_pool.execute("DELETE FROM todoist_tasks WHERE id LIKE 'tm-%'")
    await db_pool.execute("DELETE FROM todoist_outbox WHERE temp_id LIKE 'agent-task-%'")
    await db_pool.execute(
        "INSERT INTO todoist_tasks (id, content, labels, source_tag, assignee_label, is_completed) "
        "VALUES ('tm-1','a task', ARRAY['@pandora'], '#alert', '@pandora', false)"
    )
    yield
    await db_pool.execute("DELETE FROM todoist_tasks WHERE id LIKE 'tm-%'")
    await db_pool.execute("DELETE FROM todoist_outbox WHERE temp_id LIKE 'agent-task-%'")


async def test_park_task_adds_waiting_label_locally_and_to_outbox(db_pool, _seed):
    act = AgentTaskActivities(db_pool=db_pool)
    assert (await act.park_task("tm-1", "needs a human"))["parked"] is True

    labels = await db_pool.fetchval("SELECT labels FROM todoist_tasks WHERE id = 'tm-1'")
    assert PARK_LABEL in labels
    queued = await db_pool.fetchval(
        "SELECT count(*) FROM todoist_outbox WHERE temp_id = 'agent-task-park-tm-1'"
    )
    assert queued == 1


async def test_park_task_is_idempotent(db_pool, _seed):
    act = AgentTaskActivities(db_pool=db_pool)
    await act.park_task("tm-1", "first")
    await act.park_task("tm-1", "second")
    labels = await db_pool.fetchval("SELECT labels FROM todoist_tasks WHERE id = 'tm-1'")
    assert labels.count(PARK_LABEL) == 1


async def test_complete_task_queues_item_complete(db_pool, _seed):
    act = AgentTaskActivities(db_pool=db_pool)
    assert (await act.complete_task("tm-1"))["completed"] is True
    cmd = await db_pool.fetchval(
        "SELECT command FROM todoist_outbox WHERE temp_id = 'agent-task-complete-tm-1'"
    )
    assert cmd["type"] == "item_complete"


async def test_park_missing_task_is_false_not_crash(db_pool, _seed):
    assert (await AgentTaskActivities(db_pool=db_pool).park_task("tm-absent", "x"))["parked"] is False


async def test_park_task_requeues_once_the_first_park_has_drained(db_pool, _seed):
    """Regression: temp_id is deterministic and permanent per task
    (`agent-task-park-{task_id}`). If a previous park already drained to a
    terminal outbox status (committed/failed) and the human/some other flow
    then removed @waiting — returning the task to the pool — a later
    park_task call must re-queue, not silently no-op forever while only the
    local label projection changes (which the next TodoistSyncFlow pull then
    overwrites via its `labels = EXCLUDED.labels` upsert)."""
    act = AgentTaskActivities(db_pool=db_pool)
    assert (await act.park_task("tm-1", "first park"))["parked"] is True

    # Simulate TodoistSyncFlow having drained the first park, and @waiting
    # having been removed again (task re-entered the pool).
    await db_pool.execute(
        "UPDATE todoist_outbox SET status = 'committed', committed_id = 'note-1' "
        "WHERE temp_id = 'agent-task-park-tm-1'"
    )
    await db_pool.execute("UPDATE todoist_tasks SET labels = ARRAY['@pandora'] WHERE id = 'tm-1'")

    assert (await act.park_task("tm-1", "second park"))["parked"] is True

    row = await db_pool.fetchrow(
        "SELECT status, attempt_count, command FROM todoist_outbox "
        "WHERE temp_id = 'agent-task-park-tm-1'"
    )
    assert row["status"] == "pending"
    assert row["attempt_count"] == 0
    assert row["command"]["type"] == "item_update"
    # Still exactly one row for the deterministic temp_id — re-queued in
    # place, not duplicated.
    count = await db_pool.fetchval(
        "SELECT count(*) FROM todoist_outbox WHERE temp_id = 'agent-task-park-tm-1'"
    )
    assert count == 1


async def test_queue_command_leaves_an_undrained_pending_row_untouched(db_pool, _seed):
    """The other half of the fix: an outbox row still 'pending' (not yet
    drained by TodoistSyncFlow) must NOT be clobbered by a second write to
    the same temp_id — that would race a real in-flight sync attempt."""
    act = AgentTaskActivities(db_pool=db_pool)
    temp_id = "agent-task-park-tm-1"
    await act._queue_command(temp_id, {"type": "item_update", "args": {"id": "tm-1", "n": 1}})
    await act._queue_command(temp_id, {"type": "item_update", "args": {"id": "tm-1", "n": 2}})

    row = await db_pool.fetchrow(
        "SELECT status, command FROM todoist_outbox WHERE temp_id = $1", temp_id
    )
    assert row["status"] == "pending"
    assert row["command"]["args"]["n"] == 1


async def test_comment_body_carries_the_workflow_run_footer(db_pool, _seed):
    """Without this marker clarify treats the comment as fresh user input and
    re-spawns the flow every 15 minutes (loop shipped twice: 2026-05-21, 05-27).

    The fake connector exposes `commands()` (the real TodoistConnector
    interface — see build_note_add_command / check_sync_status usage
    throughout activities/alerts.py::post_task_note), not a nonexistent
    `add_note()`.
    """
    sent: dict = {}

    class _Todoist:
        @staticmethod
        async def commands(cmds: list[dict]) -> dict:
            sent["cmds"] = cmds
            return {"ok": True}

    act = AgentTaskActivities(db_pool=db_pool, todoist_connector=_Todoist())
    assert (await act.comment("tm-1", "pandoras-actor", "found the cause"))["ok"] is True
    # Prove the fake was actually exercised — a connector that silently
    # no-oped would otherwise still pass every assertion below.
    assert len(sent["cmds"]) == 1
    assert sent["cmds"][0]["type"] == "note_add"
    content = sent["cmds"][0]["args"]["content"]
    assert sent["cmds"][0]["args"]["item_id"] == "tm-1"
    assert "Workflow run:" in content
    assert "found the cause" in content


# --- comment() retry on transient envelope failure (issue #159) ---


class _FlakyTodoist:
    """Fails the envelope `fail_first` times, then succeeds.

    Mirrors the real TodoistConnector surface: `commands()` returning a Sync API
    envelope. A live http_503 lost both comments on this flow's first production
    tick, which is what the retry exists for.
    """

    def __init__(self, fail_first: int):
        self.fail_first = fail_first
        self.calls: list[list[dict]] = []

    async def commands(self, cmds: list[dict]) -> dict:
        self.calls.append(cmds)
        if len(self.calls) <= self.fail_first:
            return {"error": "http_503"}
        return {"ok": True}


async def test_comment_retries_a_transient_envelope_failure(db_pool, _seed):
    """A 503 on the first attempt must not lose the comment — it is the only
    user-visible explanation on a parked task."""
    conn = _FlakyTodoist(fail_first=1)
    act = AgentTaskActivities(db_pool=db_pool, todoist_connector=conn)

    result = await act.comment("tm-1", "pandoras-actor", "found the cause")

    assert result["ok"] is True
    assert len(conn.calls) == 2, "expected one retry after the 503"


async def test_comment_retry_reuses_the_same_command_uuid(db_pool, _seed):
    """The Sync API keys idempotency on the command uuid. Rebuilding the command
    per attempt (which a Temporal activity retry would do) could double-post."""
    conn = _FlakyTodoist(fail_first=2)
    act = AgentTaskActivities(db_pool=db_pool, todoist_connector=conn)

    await act.comment("tm-1", "pandoras-actor", "found the cause")

    uuids = {batch[0]["uuid"] for batch in conn.calls}
    assert len(conn.calls) == 3
    assert len(uuids) == 1, f"uuid changed across attempts: {uuids}"


async def test_comment_gives_up_after_the_attempt_bound(db_pool, _seed):
    conn = _FlakyTodoist(fail_first=99)
    act = AgentTaskActivities(db_pool=db_pool, todoist_connector=conn)

    result = await act.comment("tm-1", "pandoras-actor", "found the cause")

    assert result["ok"] is False
    assert len(conn.calls) == 3, "must stop at the attempt bound, not loop"


async def test_comment_does_not_retry_a_per_command_rejection(db_pool, _seed):
    """A per-command rejection is a permanent 4xx-class verdict (bad item id,
    malformed content). Retrying it poison-loops — the same lesson as
    TodoistConnector.check_sync_status's rejected_retryable distinction."""
    calls: list[list[dict]] = []

    class _Rejecting:
        @staticmethod
        async def commands(cmds: list[dict]) -> dict:
            calls.append(cmds)
            return {"sync_status": {cmds[0]["uuid"]: {"error": "item not found"}}}

    act = AgentTaskActivities(db_pool=db_pool, todoist_connector=_Rejecting())
    result = await act.comment("tm-1", "pandoras-actor", "found the cause")

    assert result["ok"] is False
    assert "command_rejected" in str(result["error"])
    assert len(calls) == 1, "a permanent rejection must NOT be retried"
