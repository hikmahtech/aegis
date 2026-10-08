"""The problem-hub chat tool: a thin, honest wrapper over the hub.

A registered tool is called the way the chat loop calls it: `(pool, args, ctx)`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from aegis.services.chat import TOOL_EXECUTORS
from aegis.services.hub import Event, get_problem, ingest_event
from aegis.services.hub_project import link_task
from aegis.services.tools.base import ToolContext
from aegis.services.tools.hub import _exec_merge_problems

pytestmark = pytest.mark.asyncio

CTX = ToolContext(agent_id="sebas")
NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


async def test_the_window_tool_left_with_the_infra_lane():
    """`set_service_state` (deploy/maintenance windows) moved to the DevOps
    vertical with the rest of the infra lane."""
    assert "set_service_state" not in TOOL_EXECUTORS


async def test_the_session_tools_left_with_the_coding_lane():
    """`task_context` and `report_progress` (the session registry) moved to the
    Development vertical with the rest of the coding lane."""
    assert "task_context" not in TOOL_EXECUTORS
    assert "report_progress" not in TOOL_EXECUTORS


# --- merge_problems -------------------------------------------------------------


def _occ(subject: str, n: int = 1) -> Event:
    return Event(
        source="flow_health",
        external_id=f"{subject}@{n}",
        kind="occurrence",
        title=f"Service {subject} down",
        klass="DockerServiceDown",
        subject=subject,
        severity="critical",
        occurred_at=NOW + timedelta(minutes=n),
    )


async def _task(pool, task_id: str, content: str = "Fix the retry policy") -> None:
    await pool.execute(
        "INSERT INTO todoist_tasks (id, content, labels, is_completed, updated_at) "
        "VALUES ($1, $2, ARRAY['@sebas'], false, now()) ON CONFLICT (id) DO NOTHING",
        task_id,
        content,
    )


async def _no_todoist(monkeypatch):
    """The projector posts comments through Todoist; here there is none, and a
    tool must still succeed — the sweep re-derives what could not be posted."""

    async def no_key(pool, settings):
        return ""

    monkeypatch.setattr("aegis.services.hub_project.resolve_todoist_api_key", no_key)


async def test_the_merge_tool_is_registered():
    assert TOOL_EXECUTORS["merge_problems"] is _exec_merge_problems


async def test_merge_problems_tool_merges_and_retires_the_duplicate_task(db_pool, monkeypatch):
    await _no_todoist(monkeypatch)
    keep_task, dup_task = f"zzk-{uuid.uuid4().hex[:6]}", f"zzd-{uuid.uuid4().hex[:6]}"
    await _task(db_pool, keep_task)
    await _task(db_pool, dup_task)
    keep = await ingest_event(db_pool, _occ(f"svc_{uuid.uuid4().hex[:8]}"), now=NOW)
    dup = await ingest_event(db_pool, _occ(f"svc_{uuid.uuid4().hex[:8]}"), now=NOW)
    await link_task(db_pool, keep.problem_id, keep_task)
    await link_task(db_pool, dup.problem_id, dup_task)

    out = await _exec_merge_problems(db_pool, {"keep_id": keep.problem_id, "merge_id": dup.problem_id}, CTX)
    # The duplicate's occurrence and its `create` state change.
    assert out.startswith(f"Merged {dup.problem_id} into {keep.problem_id}: 2 events moved")
    assert "with 2 occurrences" in out
    assert f"Task {dup_task} completed with a note." in out
    assert (await get_problem(db_pool, dup.problem_id))["status"] == "closed"
    assert await db_pool.fetchval("SELECT is_completed FROM todoist_tasks WHERE id = $1", dup_task) is True
    queued = await db_pool.fetchval(
        "SELECT count(*) FROM todoist_outbox WHERE temp_id = $1", f"problem-close-{dup_task}"
    )
    assert queued == 1

    assert (await _exec_merge_problems(db_pool, {"keep_id": "x", "merge_id": dup.problem_id}, CTX)).startswith("Refused: keep_id and merge_id must be")
    assert (await _exec_merge_problems(db_pool, {"keep_id": keep.problem_id, "merge_id": keep.problem_id}, CTX)).startswith("Refused: keep_id and merge_id are the same")
