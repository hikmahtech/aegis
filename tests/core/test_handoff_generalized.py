"""Issue #36 PR 3 — handoff_task delegates to any active agent's mention alias,
not a hardcoded 5-value enum. Validation is DB-derived (@me + aliases)."""

from __future__ import annotations

import pytest
from aegis.services.chat import TOOL_EXECUTORS, ToolContext, _assignee_labels


@pytest.mark.asyncio
async def test_assignee_labels_fallback_without_pool():
    labels = await _assignee_labels(None)
    assert labels == ["@me", "@sebas", "@raphael", "@maou", "@pandora"]


@pytest.mark.asyncio
async def test_assignee_labels_from_db_include_seed_aliases(db_pool):
    labels = await _assignee_labels(db_pool)
    assert "@me" in labels
    # Seed aliases: sebas/raphael/maou default to their id, pandora is explicit.
    for lab in ("@sebas", "@raphael", "@maou", "@pandora"):
        assert lab in labels


@pytest.mark.asyncio
async def test_handoff_rejects_unknown_assignee(db_pool):
    executor = TOOL_EXECUTORS["handoff_task"]
    result = await executor(
        db_pool, {"task_id": "t1", "to_assignee": "@nope"}, ToolContext(agent_id="sebas")
    )
    assert result.startswith("Refused: to_assignee must be one of")
    assert "@sebas" in result  # lists the valid labels


@pytest.mark.asyncio
async def test_handoff_accepts_custom_agent_alias(db_pool):
    """A custom agent's mention alias passes validation (no longer blocked by
    the old 5-value enum) — it then proceeds to the task lookup."""
    await db_pool.execute(
        """
        INSERT INTO agents (id, name, role, system_prompt_path, capabilities,
                            model_tier, metadata, active)
        VALUES ('tagtest-ops', 'tagtest-ops', 'test', '', '["research"]'::jsonb,
                'balanced', '{"mention_aliases": ["ops"]}'::jsonb, TRUE)
        ON CONFLICT (id) DO NOTHING
        """
    )
    try:
        executor = TOOL_EXECUTORS["handoff_task"]
        result = await executor(
            db_pool,
            {"task_id": "no-such-task", "to_assignee": "@ops"},
            ToolContext(agent_id="sebas"),
        )
        # Passed validation (would have said "Refused: to_assignee..." otherwise);
        # fails later at the task lookup instead.
        assert not result.startswith("Refused: to_assignee")
        assert "no-such-task" in result  # "Unknown task no-such-task"
    finally:
        await db_pool.execute("DELETE FROM agents WHERE id = 'tagtest-ops'")
