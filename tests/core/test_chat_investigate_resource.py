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


def _task_id() -> str:
    import uuid

    return f"zzir{uuid.uuid4().hex[:10]}"


async def _registered_repo(db_pool) -> str:
    """A real `resources` row the tool's repo check accepts; returns its name."""
    import uuid

    name = f"zzrepo-{uuid.uuid4().hex[:8]}"
    await db_pool.execute(
        "INSERT INTO resources (kind, slug, title, metadata) VALUES ('repository', $1, $1, $2)",
        name,
        {"github_repo": f"acme/{name}"},
    )
    return name


async def _problem_owning(db_pool, task_id: str, *, closed: bool = False) -> str:
    """A hub problem whose task is `task_id`, made the way the hub makes one."""
    import uuid
    from datetime import UTC, datetime

    from aegis.services.hub import Event, ingest_event
    from aegis.services.hub_project import link_task

    now = datetime.now(UTC)
    result = await ingest_event(
        db_pool,
        Event(
            source="heartbeat",
            external_id=f"zz-{uuid.uuid4().hex}",
            kind="occurrence",
            title="Service aegis_core down",
            klass="DockerServiceDown",
            subject=f"zzsvc_{uuid.uuid4().hex[:8]}",
            occurred_at=now,
        ),
        now=now,
    )
    assert await link_task(db_pool, result.problem_id, task_id)
    if closed:
        await db_pool.execute(
            "UPDATE problems SET status = 'closed', closed_at = now() WHERE id = $1::uuid",
            result.problem_id,
        )
    return result.problem_id


async def _started_alert(db_pool, task_id: str, repo: str) -> dict:
    temporal = MagicMock()
    temporal.start_workflow = AsyncMock(return_value=None)
    ctx = ToolContext(temporal_client=temporal, task_id=task_id)
    out = json.loads(
        await _exec_investigate_resource(db_pool, {"repo": repo, "focus": "why is it down"}, ctx)
    )
    assert out["status"] == "investigation_started", out
    return temporal.start_workflow.await_args.args[1]


async def test_a_task_with_a_problem_is_investigated_on_that_problem(db_pool):
    """#472: without the problem id the flow's step 0 ingests a fresh event,
    which creates a second problem and links this task to it too."""
    repo, task_id = await _registered_repo(db_pool), _task_id()
    problem_id = await _problem_owning(db_pool, task_id)
    alert = await _started_alert(db_pool, task_id, repo)
    assert alert["problem_id"] == problem_id
    assert alert["todoist_task_id"] == task_id


async def test_a_task_with_no_problem_still_starts_fresh(db_pool):
    repo = await _registered_repo(db_pool)
    alert = await _started_alert(db_pool, _task_id(), repo)
    assert "problem_id" not in alert


async def test_a_task_whose_problem_closed_starts_fresh(db_pool):
    """A closed problem is history: its projection is over, so the new
    investigation gets a problem of its own (the `ensure_problem_for_task`
    rule)."""
    repo, task_id = await _registered_repo(db_pool), _task_id()
    await _problem_owning(db_pool, task_id, closed=True)
    alert = await _started_alert(db_pool, task_id, repo)
    assert "problem_id" not in alert


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
