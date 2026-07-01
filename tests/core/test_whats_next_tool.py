"""whats_next chat tool + weekly_review trigger registration."""
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
from aegis.services.workflows import TRIGGERABLE_WORKFLOWS


def test_weekly_review_is_triggerable():
    assert TRIGGERABLE_WORKFLOWS["weekly_review"]["workflow"] == "WeeklyReviewFlow"
    assert TRIGGERABLE_WORKFLOWS["weekly_review"]["task_queue"] == "aegis-main"


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
        for pid in ("P_INB_N", "P_NXT_N", "P_SOM_N"):
            await conn.execute(
                "INSERT INTO todoist_projects (id, name, is_managed, raw) "
                "VALUES ($1,$1,true,'{}'::jsonb) ON CONFLICT (id) DO NOTHING", pid,
            )
        await conn.execute("DELETE FROM todoist_tasks WHERE project_id LIKE 'P_%_N'")
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, assignee_label, is_completed, raw) VALUES "
            "('N_5','P_NXT_N','quick email','{@me,@5min}','@me',false,'{}'::jsonb), "
            "('N_DEEP','P_NXT_N','deep work','{@me,@deep}','@me',false,'{}'::jsonb)"
        )
    out = await _exec_whats_next(db_pool, {"minutes": 5}, ToolContext())
    assert "quick email" in out
    assert "deep work" not in out
