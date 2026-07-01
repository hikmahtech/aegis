"""Test the list_interactions chat tool against a real Postgres fixture."""

from __future__ import annotations

import json

import pytest
from aegis.services.chat import TOOL_EXECUTORS, ToolContext


@pytest.mark.asyncio
async def test_list_interactions_returns_pending_rows(db_pool):
    """Given a seeded interaction, the tool returns it in the result list."""
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM interactions")
        await conn.execute(
            """
            INSERT INTO interactions (
                flow_run_id, agent_id, kind, origin, prompt, status, created_at
            ) VALUES
                ('flow-1', 'sebas', 'approval', 'test', 'approve?', 'pending', now()),
                ('flow-2', 'sebas', 'choice', 'test', 'pick one', 'resolved', now() - interval '1 hour'),
                ('flow-3', 'raphael', 'input', 'test', 'need input', 'pending', now())
            """
        )

    executor = TOOL_EXECUTORS["list_interactions"]
    ctx = ToolContext(agent_id="sebas")
    result = json.loads(await executor(db_pool, {"status": "pending"}, ctx))

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["prompt"] == "approve?"
    assert result[0]["kind"] == "approval"


@pytest.mark.asyncio
async def test_list_interactions_agent_id_override(db_pool):
    """Explicit agent_id argument overrides the ctx agent_id."""
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM interactions")
        await conn.execute(
            """
            INSERT INTO interactions (flow_run_id, agent_id, kind, origin, prompt, status)
            VALUES ('flow-1', 'sebas', 'approval', 'test', 'A', 'pending'),
                   ('flow-2', 'raphael', 'approval', 'test', 'B', 'pending')
            """
        )

    executor = TOOL_EXECUTORS["list_interactions"]
    ctx = ToolContext(agent_id="sebas")
    result = json.loads(await executor(db_pool, {"agent_id": "raphael", "status": "pending"}, ctx))

    assert len(result) == 1
    assert result[0]["prompt"] == "B"


@pytest.mark.asyncio
async def test_list_interactions_respects_limit(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM interactions")
        for i in range(5):
            await conn.execute(
                """
                INSERT INTO interactions (flow_run_id, agent_id, kind, origin, prompt, status)
                VALUES ($1, 'sebas', 'approval', 'test', $2, 'pending')
                """,
                f"flow-{i}",
                f"prompt-{i}",
            )

    executor = TOOL_EXECUTORS["list_interactions"]
    ctx = ToolContext(agent_id="sebas")
    result = json.loads(await executor(db_pool, {"limit": 2}, ctx))
    assert len(result) == 2
