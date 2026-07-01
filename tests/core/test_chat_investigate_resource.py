"""Tests for the investigate_resource chat tool (pandora-initiated AlertInvestigationFlow)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from aegis.services.chat import (
    AGENT_TOOL_SETS,
    CHAT_TOOLS,
    TOOL_EXECUTORS,
    ToolContext,
    _exec_investigate_resource,
)
from temporalio.exceptions import WorkflowAlreadyStartedError


def test_toolcontext_has_task_id_field_defaulting_none():
    ctx = ToolContext()
    assert ctx.task_id is None
    ctx2 = ToolContext(task_id="6gjqJrxmvp9JjGcv")
    assert ctx2.task_id == "6gjqJrxmvp9JjGcv"


def test_investigate_resource_registered_and_pandora_only():
    names = {t["function"]["name"] for t in CHAT_TOOLS}
    assert "investigate_resource" in names
    assert "investigate_resource" in TOOL_EXECUTORS
    assert "investigate_resource" in AGENT_TOOL_SETS["pandoras-actor"]
    # Scoped to pandora — the other agents must not see a fix-capable kimi trigger.
    assert "investigate_resource" not in AGENT_TOOL_SETS["sebas"]
    assert "investigate_resource" not in AGENT_TOOL_SETS["raphael"]
    assert "investigate_resource" not in AGENT_TOOL_SETS.get("maou", set())


def _pool_with_resources(rows):
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=rows)
    return pool


async def test_happy_path_spawns_alert_investigation_flow():
    pool = _pool_with_resources([{"gh": "acme/bcp", "rp": None}])
    temporal = MagicMock()
    temporal.start_workflow = AsyncMock(return_value=None)
    ctx = ToolContext(temporal_client=temporal, task_id="6gjqJrxmvp9JjGcv")

    out = json.loads(
        await _exec_investigate_resource(
            pool, {"repo": "bcp", "focus": "exec_info TypeError"}, ctx
        )
    )

    assert out["status"] == "investigation_started"
    assert out["repo"] == "bcp"
    # Workflow id is deterministic — no random suffix.
    assert out["workflow_id"] == "chat-investigate-6gjqJrxmvp9JjGcv"
    temporal.start_workflow.assert_awaited_once()
    call = temporal.start_workflow.await_args
    assert call.args[0] == "AlertInvestigationFlow"
    alert = call.args[1]
    assert alert["source"] == "todoist-chat"  # non-Jira → Gate-2 + fix-capable kimi
    assert alert["service"] == "bcp"
    assert alert["todoist_task_id"] == "6gjqJrxmvp9JjGcv"
    assert alert["requires_approval"] is False
    # Fingerprint is also deterministic per task.
    assert alert["fingerprint"] == "chat-investigate-6gjqJrxmvp9JjGcv"
    assert call.kwargs["task_queue"] == "aegis-main"
    assert call.kwargs["id"] == "chat-investigate-6gjqJrxmvp9JjGcv"
    # Guard the resources metadata key — it's `path`, not `resource_path`
    # (regression guard: a wrong key silently NULLs the rp column).
    fetch_sql = pool.fetch.await_args.args[0]
    assert "metadata->>'path'" in fetch_sql
    assert "resource_path" not in fetch_sql


async def test_unknown_repo_returns_available_and_does_not_spawn():
    pool = _pool_with_resources([{"gh": "acme/bcp", "rp": None}])
    temporal = MagicMock()
    temporal.start_workflow = AsyncMock()
    ctx = ToolContext(temporal_client=temporal, task_id="t1")

    out = json.loads(
        await _exec_investigate_resource(pool, {"repo": "nope", "focus": "x"}, ctx)
    )

    assert "error" in out
    assert "bcp" in out["available_repos"]
    temporal.start_workflow.assert_not_awaited()


async def test_missing_task_id_refuses_and_does_not_spawn():
    pool = _pool_with_resources([{"gh": "acme/bcp", "rp": None}])
    temporal = MagicMock()
    temporal.start_workflow = AsyncMock()
    ctx = ToolContext(temporal_client=temporal, task_id=None)

    out = json.loads(
        await _exec_investigate_resource(pool, {"repo": "bcp", "focus": "x"}, ctx)
    )

    assert "error" in out
    assert "Todoist task" in out["error"]
    temporal.start_workflow.assert_not_awaited()


async def test_missing_repo_or_focus_refuses():
    pool = _pool_with_resources([])
    ctx = ToolContext(temporal_client=MagicMock(), task_id="t1")
    out = json.loads(await _exec_investigate_resource(pool, {"repo": "", "focus": ""}, ctx))
    assert "error" in out


async def test_no_temporal_client_refuses():
    pool = _pool_with_resources([{"gh": "acme/bcp", "rp": None}])
    ctx = ToolContext(temporal_client=None, task_id="t1")
    out = json.loads(
        await _exec_investigate_resource(pool, {"repo": "bcp", "focus": "x"}, ctx)
    )
    assert "error" in out


async def test_resource_path_only_resource_matches():
    pool = _pool_with_resources([{"gh": None, "rp": "bcp"}])
    temporal = MagicMock()
    temporal.start_workflow = AsyncMock(return_value=None)
    ctx = ToolContext(temporal_client=temporal, task_id="t1")
    out = json.loads(
        await _exec_investigate_resource(pool, {"repo": "bcp", "focus": "x"}, ctx)
    )
    assert out["status"] == "investigation_started"
    temporal.start_workflow.assert_awaited_once()


async def test_resource_lookup_failure_degrades_to_error():
    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=RuntimeError("db down"))
    temporal = MagicMock()
    temporal.start_workflow = AsyncMock()
    ctx = ToolContext(temporal_client=temporal, task_id="t1")
    out = json.loads(
        await _exec_investigate_resource(pool, {"repo": "bcp", "focus": "x"}, ctx)
    )
    assert "error" in out
    temporal.start_workflow.assert_not_awaited()


async def test_investigate_resource_dedups_when_already_running():
    """A duplicate call while the workflow is in-flight returns already_investigating, not an error."""
    pool = _pool_with_resources([{"gh": "acme/bcp", "rp": None}])
    temporal = MagicMock()
    temporal.start_workflow = AsyncMock(
        side_effect=WorkflowAlreadyStartedError(
            "chat-investigate-taskABC", "AlertInvestigationFlow"
        )
    )
    ctx = ToolContext(temporal_client=temporal, task_id="taskABC")

    out = json.loads(
        await _exec_investigate_resource(pool, {"repo": "bcp", "focus": "some bug"}, ctx)
    )

    assert out["status"] == "already_investigating"
    assert out["workflow_id"] == "chat-investigate-taskABC"
    assert out["repo"] == "bcp"
    assert "error" not in out
