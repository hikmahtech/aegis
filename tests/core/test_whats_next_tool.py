"""whats_next chat tool."""
from __future__ import annotations

import pytest
from aegis.db import run_migrations
from aegis.services.chat import (
    AGENT_TOOL_SETS,
    TOOL_EXECUTORS,
    ToolContext,
    _exec_whats_next,
    _validate_agent_tool_sets,
)


def test_whats_next_registered_and_valid():
    assert TOOL_EXECUTORS["whats_next"] is _exec_whats_next
    assert "whats_next" in AGENT_TOOL_SETS["sebas"]
    _validate_agent_tool_sets()  # must not raise


@pytest.mark.asyncio
async def test_whats_next_filters_by_context(db_pool):
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"inbox": "P_INB_N", "next": "P_NXT_N", "someday": "P_SOM_N"},
        )
        projects = ["P_INB_N", "P_NXT_N", "P_SOM_N"]
        for pid in projects:
            await conn.execute(
                "INSERT INTO todoist_projects (id, name, is_managed, raw) "
                "VALUES ($1,$1,true,'{}'::jsonb) ON CONFLICT (id) DO NOTHING", pid,
            )
        # Exact ids, children before parents. The previous sweep was
        # `LIKE 'P_%_N'`, where `_` is a single-char WILDCARD, not a literal —
        # so it also matched 'PRJ_FN' and deleted TASK_FN, a fixture row owned
        # by tests/worker/test_clarify_activities.py. That row has
        # todoist_notes children, so under `-n auto --dist loadfile` the
        # sweep raised ForeignKeyViolationError whenever the two files landed
        # on the same xdist worker.
        await conn.execute(
            "DELETE FROM todoist_notes WHERE item_id IN "
            "(SELECT id FROM todoist_tasks WHERE project_id = ANY($1::text[]))",
            projects,
        )
        await conn.execute(
            "DELETE FROM todoist_tasks WHERE project_id = ANY($1::text[])", projects
        )
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, assignee_label, is_completed, raw) VALUES "
            "('N_5','P_NXT_N','quick email','{@me,@5min}','@me',false,'{}'::jsonb), "
            "('N_DEEP','P_NXT_N','deep work','{@me,@deep}','@me',false,'{}'::jsonb)"
        )
    out = await _exec_whats_next(db_pool, {"minutes": 5}, ToolContext())
    assert "quick email" in out
    assert "deep work" not in out
