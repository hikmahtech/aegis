"""AgentTaskActivities.find_actionable_tasks — eligibility + backlog brake."""

from __future__ import annotations

import pytest_asyncio
from aegis_worker.activities.agent_task import AgentTaskActivities

_IDS = tuple(f"tt-{n}" for n in range(1, 10))


@pytest_asyncio.fixture(loop_scope="function")
async def _seed(db_pool):
    await db_pool.execute("DELETE FROM todoist_tasks WHERE id = ANY($1::text[])", list(_IDS))
    await db_pool.execute(
        """
        INSERT INTO todoist_tasks
            (id, content, labels, source_tag, assignee_label, is_completed, updated_at)
        VALUES
          ('tt-1','alert oldest',   ARRAY['@pandora'],           '#alert',  '@pandora', false, now() - interval '9 days'),
          ('tt-2','email mid',      ARRAY['@sebas'],             '#email',  '@sebas',   false, now() - interval '8 days'),
          ('tt-3','receipt newer',  ARRAY['@maou'],              '#receipt','@maou',    false, now() - interval '7 days'),
          ('tt-4','someday',        ARRAY['@pandora','@someday'],'#alert',  '@pandora', false, now() - interval '10 days'),
          ('tt-5','waiting',        ARRAY['@pandora','@waiting'],'#alert',  '@pandora', false, now() - interval '10 days'),
          ('tt-6','done',           ARRAY['@pandora'],           '#alert',  '@pandora', true,  now() - interval '10 days'),
          ('tt-7','no assignee',    ARRAY['@next'],              '#alert',  NULL,       false, now() - interval '10 days'),
          ('tt-8','user code task', ARRAY['@pandora','@code'],   NULL,      '@pandora', false, now() - interval '6 days'),
          ('tt-9','dateless alert', ARRAY['@pandora'],           '#alert',  '@pandora', false, now() - interval '5 days')
        """
    )
    yield
    await db_pool.execute("DELETE FROM todoist_tasks WHERE id = ANY($1::text[])", list(_IDS))
    await db_pool.execute("DELETE FROM workflow_runs WHERE todoist_task_ref = ANY($1::text[])", list(_IDS))


async def test_selects_dateless_agent_tasks_oldest_first(db_pool, _seed):
    """No due date is required — none of the 80 real tasks has one."""
    act = AgentTaskActivities(db_pool=db_pool)
    rows = await act.find_actionable_tasks(max_tasks=3)
    assert [r["id"] for r in rows] == ["tt-1", "tt-2", "tt-3"]


async def test_excludes_someday_waiting_completed_and_unassigned(db_pool, _seed):
    act = AgentTaskActivities(db_pool=db_pool)
    ids = {r["id"] for r in await act.find_actionable_tasks(max_tasks=50)}
    assert "tt-4" not in ids  # @someday
    assert "tt-5" not in ids  # @waiting — the parking state must exit the pool
    assert "tt-6" not in ids  # completed
    assert "tt-7" not in ids  # no assignee label


async def test_cap_respected(db_pool, _seed):
    act = AgentTaskActivities(db_pool=db_pool)
    assert len(await act.find_actionable_tasks(max_tasks=2)) == 2


async def test_cooldown_excludes_recently_run_task(db_pool, _seed):
    await db_pool.execute(
        """
        INSERT INTO workflow_runs (run_id, workflow_id, workflow_type, status, started_at, todoist_task_ref)
        VALUES ('r1','agent-task-tt-1','AgentTaskFlow','completed', now() - interval '1 hour', 'tt-1')
        """
    )
    act = AgentTaskActivities(db_pool=db_pool)
    ids = [r["id"] for r in await act.find_actionable_tasks(max_tasks=3)]
    assert "tt-1" not in ids


async def test_cooldown_expired_task_is_eligible_again(db_pool, _seed):
    await db_pool.execute(
        """
        INSERT INTO workflow_runs (run_id, workflow_id, workflow_type, status, started_at, todoist_task_ref)
        VALUES ('r2','agent-task-tt-1','AgentTaskFlow','completed', now() - interval '7 hours', 'tt-1')
        """
    )
    act = AgentTaskActivities(db_pool=db_pool)
    assert "tt-1" in [r["id"] for r in await act.find_actionable_tasks(max_tasks=3)]


async def test_at_most_one_coding_task_per_batch(db_pool, _seed):
    """Coding runs take minutes and the tmux window cap is 10."""
    await db_pool.execute(
        """
        INSERT INTO todoist_tasks (id, content, labels, source_tag, assignee_label, is_completed, updated_at)
        VALUES ('tt-10','code b', ARRAY['@pandora','@code'], NULL, '@pandora', false, now() - interval '11 days')
        """
    )
    try:
        act = AgentTaskActivities(db_pool=db_pool)
        rows = await act.find_actionable_tasks(max_tasks=5, max_coding=1)
        coding = [r for r in rows if r["source_tag"] is None and "@code" in r["labels"]]
        assert len(coding) == 1
    finally:
        await db_pool.execute("DELETE FROM todoist_tasks WHERE id = 'tt-10'")


async def test_no_pool_degrades_to_empty():
    assert await AgentTaskActivities(db_pool=None).find_actionable_tasks() == []


async def test_coding_backlog_does_not_underfill_batch(db_pool, _seed):
    """Regression: a large, old coding backlog must not starve non-coding tasks
    out of the batch just because the SQL scan window fills entirely with
    coding rows before the per-batch coding cap is applied in Python.
    """
    coding_ids = [f"ct-{n}" for n in range(1, 21)]
    await db_pool.executemany(
        """
        INSERT INTO todoist_tasks
            (id, content, labels, source_tag, assignee_label, is_completed, updated_at)
        VALUES ($1, $2, ARRAY['@pandora','@code'], NULL, '@pandora', false, now() - make_interval(days => $3))
        """,
        [(cid, f"coding backlog {n}", 20 + n) for n, cid in enumerate(coding_ids, start=1)],
    )
    try:
        act = AgentTaskActivities(db_pool=db_pool)
        rows = await act.find_actionable_tasks(max_tasks=3, max_coding=1)
        coding = [r for r in rows if r["source_tag"] is None and "@code" in r["labels"]]
        assert len(rows) == 3
        assert len(coding) == 1
    finally:
        await db_pool.execute("DELETE FROM todoist_tasks WHERE id = ANY($1::text[])", coding_ids)


async def test_a_task_with_a_session_row_leaves_the_pool(db_pool, _seed):
    """The sweep only ever starts TURN ONE. Later turns arrive through
    `find_task_turns_due`, keyed on `last_turn_at`, so a task that already has a
    session must drop out here — otherwise every 15-minute tick would start a
    second first turn on a conversation that is already going.
    """
    await db_pool.execute(
        "INSERT INTO task_sessions (task_id, agent_id, session_id) "
        "VALUES ('tt-8', 'pandoras-actor', gen_random_uuid())"
    )
    try:
        act = AgentTaskActivities(db_pool=db_pool)
        ids = {r["id"] for r in await act.find_actionable_tasks(max_tasks=50)}
        assert "tt-8" not in ids
        # Scoped to the task that has the row — nothing else drops out.
        assert {"tt-1", "tt-2", "tt-3"} <= ids
    finally:
        await db_pool.execute("DELETE FROM task_sessions WHERE task_id = 'tt-8'")
