"""Tests for the `system_status` chat tool (schema + dispatch wiring)."""

from __future__ import annotations

import json

from aegis.services.chat import AGENT_TOOL_SETS, CHAT_TOOLS, TOOL_EXECUTORS, ToolContext


def test_system_status_schema_registered():
    names = {t["function"]["name"] for t in CHAT_TOOLS}
    assert "system_status" in names


def test_system_status_has_executor():
    assert "system_status" in TOOL_EXECUTORS


def test_sebas_can_call_system_status():
    assert "system_status" in AGENT_TOOL_SETS["sebas"]


async def test_system_status_executor_returns_digest_shape(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM workflow_runs")
        await conn.execute(
            "INSERT INTO workflow_runs "
            "(run_id, workflow_id, workflow_type, status, started_at, completed_at) VALUES "
            "('cst-r1','cst-w1','DailyBriefingFlow','completed', now(), now())"
        )

    executor = TOOL_EXECUTORS["system_status"]
    ctx = ToolContext(agent_id="sebas")
    result = json.loads(await executor(db_pool, {"hours": 24}, ctx))

    assert result["window_hours"] == 24
    for key in (
        "runs_by_type_status",
        "failed_runs",
        "completed_but_failed",
        "llm_calls",
        "llm_tokens",
        "pending_interactions",
        "infra_stuck",
        "infra_confirmed",
    ):
        assert key in result

    await db_pool.execute("DELETE FROM workflow_runs WHERE run_id = 'cst-r1'")


async def test_system_status_defaults_to_24_hours_and_clamps(db_pool):
    executor = TOOL_EXECUTORS["system_status"]
    ctx = ToolContext(agent_id="sebas")

    result = json.loads(await executor(db_pool, {}, ctx))
    assert result["window_hours"] == 24

    # Clamped to the 168h (7 day) max rather than blowing up the query window.
    result = json.loads(await executor(db_pool, {"hours": 10_000}, ctx))
    assert result["window_hours"] == 168
