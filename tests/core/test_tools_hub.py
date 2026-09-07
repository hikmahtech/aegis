"""The problem-hub chat tools: thin, honest wrappers over the hub and the
session registry.

A registered tool is called the way the chat loop calls it: `(pool, args, ctx)`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from aegis.services import hub_project, work_sessions
from aegis.services.chat import TOOL_EXECUTORS
from aegis.services.hub import (
    Event,
    find_problem_for_task,
    get_problem,
    ingest_event,
    list_events,
    list_service_states,
)
from aegis.services.hub_project import link_task
from aegis.services.tools.base import ToolContext
from aegis.services.tools.hub import (
    _exec_merge_problems,
    _exec_report_progress,
    _exec_set_service_state,
    _exec_task_context,
)

pytestmark = pytest.mark.asyncio

CTX = ToolContext(agent_id="pandoras-actor")
NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


async def _call(pool, **args) -> str:
    return await _exec_set_service_state(pool, args, CTX)


async def test_tool_is_registered_and_dispatches_to_the_same_function():
    assert TOOL_EXECUTORS["set_service_state"] is _exec_set_service_state


async def test_sets_a_window_and_lists_what_is_in_force(db_pool):
    s = f"svc_{uuid.uuid4().hex[:8]}"
    out = await _call(db_pool, subject=s, state="deploying", minutes=20, note="rolling core")
    assert out.startswith(f"{s}: deploying until ")
    assert "set by chat:pandoras-actor" in out
    assert f"- {s} (service): deploying until" in out
    rows = [r for r in await list_service_states(db_pool) if r["subject"] == s]
    assert rows and rows[0]["note"] == "rolling core"


async def test_ok_clears_and_says_so(db_pool):
    s = f"svc_{uuid.uuid4().hex[:8]}"
    await _call(db_pool, subject=s, state="maintenance", minutes=5)
    out = await _call(db_pool, subject=s, state="ok")
    assert out.startswith(f"{s}: window cleared.")
    out = await _call(db_pool, subject=s, state="ok")
    assert out.startswith(f"{s}: no window was set.")


async def test_star_is_a_global_window(db_pool):
    try:
        out = await _call(db_pool, subject="*", state="maintenance", minutes=1, note="power cut")
        assert "- * (*): maintenance until" in out
    finally:
        await _call(db_pool, subject="*", state="ok")


async def test_empty_subject_is_refused(db_pool):
    out = await _call(db_pool, subject="  ", state="deploying")
    assert out.startswith("Refused: subject is required")


async def test_unknown_args_are_dropped_not_fatal(db_pool):
    s = f"svc_{uuid.uuid4().hex[:8]}"
    out = await _call(db_pool, subject=s, state="degraded", bogus=1)
    assert out.startswith(f"{s}: degraded until ")
    await _call(db_pool, subject=s, state="ok")


# --- task_context / report_progress / merge_problems ---------------------------


def _occ(subject: str, n: int = 1) -> Event:
    return Event(
        source="heartbeat",
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
        "VALUES ($1, $2, ARRAY['@pandora','@code'], false, now()) ON CONFLICT (id) DO NOTHING",
        task_id,
        content,
    )


async def _no_todoist(monkeypatch):
    """The projector posts comments through Todoist; here there is none, and a
    tool must still succeed — the sweep re-derives what could not be posted."""

    async def no_key(pool, settings):
        return ""

    monkeypatch.setattr("aegis.services.hub_project.resolve_todoist_api_key", no_key)


async def test_the_three_tools_are_registered():
    assert TOOL_EXECUTORS["task_context"] is _exec_task_context
    assert TOOL_EXECUTORS["report_progress"] is _exec_report_progress
    assert TOOL_EXECUTORS["merge_problems"] is _exec_merge_problems


async def test_task_context_needs_an_id():
    out = await _exec_task_context(None, {}, CTX)
    assert out.startswith("Refused: task_id or problem_id is required")


async def test_task_context_reads_a_plain_code_task(db_pool):
    task = f"zzc-{uuid.uuid4().hex[:6]}"
    await _task(db_pool, task)
    await work_sessions.create_session(db_pool, task_id=task, agent_id="pandoras-actor")
    await work_sessions.set_repo(
        db_pool, task, repo="hikmah/aegis", github_repo="hikmahtech/aegis",
        worktree_path="/w/hikmah/aegis-aegis-wt/task-1", branch="aegis-task/1", host="meem",
    )
    await work_sessions.set_last_run(db_pool, task, output_file="/tmp/x", host="meem", account="work")
    await work_sessions.set_state(db_pool, task, status="parked", summary="waiting on you: plan")
    out = await _exec_task_context(db_pool, {"task_id": task}, CTX)
    assert out.startswith(f"Task {task}: Fix the retry policy [@pandora @code]")
    assert "No problem on the hub for this task" in out
    assert "- aegis parked (work)" in out and "waiting on you: plan" in out
    sid = (await work_sessions.get_session(db_pool, task))["session_id"]
    assert f"take over: cd /w/hikmah/aegis-aegis-wt/task-1 && CLAUDE_CONFIG_DIR=<work> claude --resume {sid}" in out


async def test_task_context_reads_a_problem_with_events_links_and_sessions(db_pool):
    task = f"zzp-{uuid.uuid4().hex[:6]}"
    s = f"svc_{uuid.uuid4().hex[:8]}"
    await _task(db_pool, task, content=f"Service {s} down")
    r = await ingest_event(db_pool, _occ(s), now=NOW)
    await link_task(db_pool, r.problem_id, task)
    await work_sessions.upsert_operator_session(
        db_pool, task_id=task, account="personal", status="active", summary="on it", problem_id=r.problem_id
    )
    by_task = await _exec_task_context(db_pool, {"task_id": task}, CTX)
    by_problem = await _exec_task_context(db_pool, {"problem_id": r.problem_id}, CTX)
    for out in (by_task, by_problem):
        assert f"Problem {r.problem_id}: Service {s} down" in out
        assert "Status: open · seen 1×" in out
        assert f"Subject: {s} (service) · critical · class dockerservicedown" in out
        assert "- operator active (personal)" in out and "on it" in out
        # The occurrence and the hub's own `create` state change.
        assert "Recent events (newest first, 2):" in out
        assert "occurrence/heartbeat" in out and "state_change/hub: create" in out
    out = await _exec_task_context(db_pool, {"problem_id": "not-a-uuid"}, CTX)
    assert out.startswith("Refused")


async def test_report_progress_registers_the_session_and_notes_the_problem(db_pool, monkeypatch):
    await _no_todoist(monkeypatch)
    task = f"zzr-{uuid.uuid4().hex[:6]}"
    s = f"svc_{uuid.uuid4().hex[:8]}"
    await _task(db_pool, task)
    r = await ingest_event(db_pool, _occ(s), now=NOW)
    await link_task(db_pool, r.problem_id, task)
    await work_sessions.create_session(db_pool, task_id=task, agent_id="pandoras-actor")

    out = await _exec_report_progress(
        db_pool,
        {"task_id": task, "summary": "found the cause, writing the fix", "account": "personal",
         "pr_url": "https://github.com/o/r/pull/7", "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
        CTX,
    )
    assert out.startswith(f"Recorded on task {task}: active (personal) — found the cause")
    assert "Linked https://github.com/o/r/pull/7." in out
    assert "AEGIS's session: aegis active" in out
    rows = await work_sessions.list_for_task(db_pool, task)
    assert [x["owner"] for x in rows] == ["aegis", "operator"]
    op = rows[1]
    assert op["account"] == "personal" and op["status"] == "active"
    assert op["session_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert op["problem_id"] == r.problem_id
    notes = [e for e in await list_events(db_pool, r.problem_id) if e["kind"] == "session_note"]
    assert len(notes) == 1 and notes[0]["payload"]["text"] == "active (personal): found the cause, writing the fix"
    assert notes[0]["payload"]["pr_url"] == "https://github.com/o/r/pull/7"
    assert await db_pool.fetchval(
        "SELECT count(*) FROM problem_links WHERE problem_id = $1::uuid AND link_kind = 'github_pr'",
        r.problem_id,
    ) == 1
    # A second report from the same account updates the row, not a new one.
    await _exec_report_progress(db_pool, {"task_id": task, "summary": "PR is up", "status": "done", "account": "personal"}, CTX)
    rows = await work_sessions.list_for_task(db_pool, task)
    assert len(rows) == 2 and rows[1]["status"] == "done" and rows[1]["summary"] == "PR is up"
    # While the operator row is active the collision lookup sees it.
    await _exec_report_progress(db_pool, {"task_id": task, "summary": "back on it", "account": "work"}, CTX)
    live = await work_sessions.live_operator_sessions(db_pool, task)
    assert [x["account"] for x in live] == ["work"]


async def test_report_progress_gives_a_plain_task_a_problem(db_pool, monkeypatch):
    await _no_todoist(monkeypatch)
    task = f"zzn-{uuid.uuid4().hex[:6]}"
    await _task(db_pool, task, content="Fix flaky cleanup test")
    out = await _exec_report_progress(db_pool, {"task_id": task, "summary": "started"}, CTX)
    assert "Created problem " in out
    p = await find_problem_for_task(db_pool, task)
    assert p is not None
    assert p["class"] == "manual" and p["subject_kind"] == "repo" and p["subject"] == f"task-{task}"
    assert p["title"] == "Fix flaky cleanup test"
    assert p["todoist_task_id"] == task
    again = await _exec_report_progress(db_pool, {"task_id": task, "summary": "more"}, CTX)
    assert "Created problem" not in again, "the second report finds the problem it made"
    assert (await find_problem_for_task(db_pool, task))["id"] == p["id"]


async def test_report_progress_refuses_bad_input(db_pool):
    assert (await _exec_report_progress(db_pool, {"task_id": "", "summary": "x"}, CTX)).startswith("Refused: task_id and summary")
    assert (await _exec_report_progress(db_pool, {"task_id": "nope-1", "summary": "  "}, CTX)).startswith("Refused: task_id and summary")
    out = await _exec_report_progress(db_pool, {"task_id": f"zz-missing-{uuid.uuid4().hex[:4]}", "summary": "x"}, CTX)
    assert "is not in the Todoist mirror" in out


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


async def test_report_progress_ticks_off_a_plan_step(db_pool, monkeypatch):
    """`step_done` is how a session says a plan step is finished: the note
    carries it, the projector completes that subtask, and the reply says where
    the checklist stands."""
    await _no_todoist(monkeypatch)
    task = f"zzs-{uuid.uuid4().hex[:6]}"
    await _task(db_pool, task)
    problem = await hub_project.ensure_problem_for_task(db_pool, task)
    assert problem is not None
    # Two steps, linked the way the projector links them.
    for index, step_task in ((1, f"{task}-s1"), (2, f"{task}-s2")):
        await _task(db_pool, step_task, content=f"step {index}")
        await db_pool.execute(
            "INSERT INTO problem_links (problem_id, link_kind, ref) "
            "VALUES ($1::uuid, 'plan_step', $2) ON CONFLICT DO NOTHING",
            problem["id"],
            f"{index}:{step_task}",
        )

    out = await _exec_report_progress(
        db_pool, {"task_id": task, "summary": "index is in", "step_done": 1}, CTX
    )
    assert "Plan steps: 1/2 done." in out
    assert await db_pool.fetchval(
        "SELECT is_completed FROM todoist_tasks WHERE id = $1", f"{task}-s1"
    ) is True
    assert await db_pool.fetchval(
        "SELECT is_completed FROM todoist_tasks WHERE id = $1", f"{task}-s2"
    ) is False
    note = [e for e in await list_events(db_pool, problem["id"]) if e["kind"] == "session_note"][0]
    assert note["payload"]["step_done"] == 1

    # No step number means no tick, and the reply still reports the checklist.
    out = await _exec_report_progress(db_pool, {"task_id": task, "summary": "thinking"}, CTX)
    assert "Plan steps: 1/2 done." in out
