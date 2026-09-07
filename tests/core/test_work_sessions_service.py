"""aegis.services.work_sessions — row lifecycle, due-turn query, dispatch dance."""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aegis.clarify_note import AGENT_REPLY_PREFIX, CLARIFY_NOTE_PREFIX
from aegis.services import work_sessions as svc
from temporalio.exceptions import WorkflowAlreadyStartedError

_TASK = "ts-task-1"


@pytest_asyncio.fixture(loop_scope="function")
async def _clean(db_pool):
    for sql in (
        "DELETE FROM work_sessions WHERE task_id = $1",
        "DELETE FROM todoist_notes WHERE item_id = $1",
        "DELETE FROM todoist_tasks WHERE id = $1",
    ):
        await db_pool.execute(sql, _TASK)
    await db_pool.execute(
        "INSERT INTO todoist_tasks (id, content, labels, is_completed, updated_at) "
        "VALUES ($1, 'fix it', ARRAY['@pandora','@code'], false, now())",
        _TASK,
    )
    yield
    for sql in (
        "DELETE FROM work_sessions WHERE task_id = $1",
        "DELETE FROM todoist_notes WHERE item_id = $1",
        "DELETE FROM todoist_tasks WHERE id = $1",
    ):
        await db_pool.execute(sql, _TASK)


async def _note(db_pool, content: str, age: str = "0 seconds"):
    # $4::text::interval, not $4::interval — a bare interval cast makes asyncpg
    # infer the parameter as an interval and demand a timedelta.
    await db_pool.execute(
        "INSERT INTO todoist_notes (id, item_id, content, posted_at, raw) "
        "VALUES ($1, $2, $3, now() - $4::text::interval, '{}')",
        str(uuid.uuid4()),
        _TASK,
        content,
        age,
    )


async def test_create_is_idempotent_and_mints_one_session_id(db_pool, _clean):
    a = await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    b = await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    assert a["session_id"] == b["session_id"]
    uuid.UUID(a["session_id"])  # a valid uuid string
    assert a["turns"] == 0 and a["repo"] == ""


async def test_set_last_run_records_where_the_turn_writes(db_pool, _clean):
    """The row starts empty, which is what "no turn on record" means to the
    collision check — it must not be NULL, and it must round-trip."""
    row = await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    assert row["last_output_file"] == "" and row["last_host"] == ""

    await svc.set_last_run(db_pool, _TASK, output_file="/tmp/aegis-kimi-run-r7.jsonl", host="meem")
    row = await svc.get_session(db_pool, _TASK)
    assert row["last_output_file"] == "/tmp/aegis-kimi-run-r7.jsonl"
    assert row["last_host"] == "meem"


async def test_set_repo_records_the_checkout(db_pool, _clean):
    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    await svc.set_repo(
        db_pool,
        _TASK,
        repo="hikmah/aegis",
        github_repo="hikmahtech/aegis",
        worktree_path="/home/a/w/aegis/.claude/worktrees/fix",
        branch="worktree-fix",
        host="meem",
    )
    row = await svc.get_session(db_pool, _TASK)
    assert row["repo"] == "hikmah/aegis"
    assert row["github_repo"] == "hikmahtech/aegis"
    assert row["worktree_path"] == "/home/a/w/aegis/.claude/worktrees/fix"
    assert row["branch"] == "worktree-fix"
    assert row["host"] == "meem"


async def test_record_turn_counts_only_launched_turns(db_pool, _clean):
    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    await svc.record_turn(db_pool, _TASK, launched=False)
    await svc.record_turn(db_pool, _TASK, launched=True)
    row = await svc.get_session(db_pool, _TASK)
    assert row["turns"] == 1 and row["last_turn_at"] is not None


async def test_find_turns_due_sees_only_user_notes_newer_than_the_watermark(db_pool, _clean):
    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    await _note(db_pool, "please also fix the tests")
    due = await svc.find_turns_due(db_pool)
    assert [(d["task_id"], d["comment"]) for d in due] == [(_TASK, "please also fix the tests")]
    assert due[0]["agent_id"] == "pandoras-actor"
    await svc.record_turn(db_pool, _TASK, launched=True)
    assert await svc.find_turns_due(db_pool) == []
    await _note(db_pool, f"[pandoras-actor] done\n\nWorkflow run: agent-task-{_TASK}")
    await _note(db_pool, CLARIFY_NOTE_PREFIX + "2026] filed")
    await _note(db_pool, AGENT_REPLY_PREFIX + "2026 agent=sebas] hi")
    assert await svc.find_turns_due(db_pool) == []


async def test_find_turns_due_joins_every_unanswered_note_oldest_first(db_pool, _clean):
    """This is the fallback for a webhook that never arrived, and an outage
    drops a RUN of comments, not one. Returning only the newest would answer the
    last message of a conversation the turn never read.

    Falsifiable: go back to `ORDER BY posted_at DESC LIMIT 1` and only the last
    note comes back.
    """
    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    # The session predates both notes, as it does in life: `created_at` is the
    # watermark until the first turn runs.
    await db_pool.execute(
        "UPDATE work_sessions SET created_at = now() - interval '1 hour' WHERE task_id = $1",
        _TASK,
    )
    await _note(db_pool, "use the other repo", age="30 seconds")
    await _note(db_pool, "and open a draft PR", age="10 seconds")

    due = await svc.find_turns_due(db_pool)
    assert len(due) == 1, "still one row per task"
    assert due[0]["comment"] == "use the other repo\n\nand open a draft PR"

    # And the watermark still ends the batch.
    await svc.record_turn(db_pool, _TASK, launched=True)
    assert await svc.find_turns_due(db_pool) == []


async def test_find_turns_due_skips_completed_tasks(db_pool, _clean):
    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    await _note(db_pool, "go")
    await db_pool.execute("UPDATE todoist_tasks SET is_completed = true WHERE id = $1", _TASK)
    assert await svc.find_turns_due(db_pool) == []


async def test_slack_ref_round_trip(db_pool, _clean):
    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    await svc.set_slack_ref(db_pool, _TASK, {"channel": "C1", "ts": "1.2"})
    # A jsonb object, not a double-encoded string scalar — `->>` reads it below.
    assert (await svc.get_session(db_pool, _TASK))["slack_ref"] == {"channel": "C1", "ts": "1.2"}
    assert await svc.find_by_thread(db_pool, "C1", "1.2") == _TASK
    assert await svc.find_by_thread(db_pool, "C1", "9.9") is None


async def test_get_session_is_none_for_an_unknown_task(db_pool, _clean):
    assert await svc.get_session(db_pool, _TASK) is None


def test_is_user_note():
    assert svc.is_user_note("please do X")
    assert not svc.is_user_note(CLARIFY_NOTE_PREFIX + "x")
    assert not svc.is_user_note(AGENT_REPLY_PREFIX + "x")
    assert not svc.is_user_note("[pandoras-actor] plan\n\nWorkflow run: agent-task-1")


@pytest.mark.asyncio
async def test_dispatch_starts_then_signals_then_restarts():
    client = MagicMock()
    client.start_workflow = AsyncMock()
    assert await svc.dispatch_task_turn(client, task_id="t1", agent_id="a", comment="go") == "started"
    kw = client.start_workflow.call_args.kwargs
    assert kw["id"] == "agent-task-t1" and kw["task_queue"] == "aegis-main"
    assert client.start_workflow.call_args.args[1]["comment"] == "go"

    handle = MagicMock()
    handle.signal = AsyncMock()
    client.get_workflow_handle = MagicMock(return_value=handle)
    client.start_workflow = AsyncMock(
        side_effect=WorkflowAlreadyStartedError("agent-task-t1", "AgentTaskFlow")
    )
    assert await svc.dispatch_task_turn(client, task_id="t1", agent_id="a", comment="more") == "signalled"
    handle.signal.assert_awaited_once_with("comment", "more")

    handle.signal = AsyncMock(side_effect=RuntimeError("workflow execution already completed"))
    client.start_workflow = AsyncMock(side_effect=[WorkflowAlreadyStartedError("x", "y"), None])
    assert await svc.dispatch_task_turn(client, task_id="t1", agent_id="a", comment="again") == "started"


# --- the registry widening (PR 5) ---------------------------------------------


async def test_set_last_run_records_account_and_engine_and_keeps_them_when_blank(db_pool, _clean):
    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    await svc.set_last_run(db_pool, _TASK, output_file="/tmp/r1", host="meem", account="work")
    row = await svc.get_session(db_pool, _TASK)
    assert row["account"] == "work" and row["engine"] == "claude" and row["status"] == "active"
    assert row["last_seen_at"] is not None
    await svc.set_last_run(db_pool, _TASK, output_file="/tmp/r2", host="meem")
    row = await svc.get_session(db_pool, _TASK)
    assert row["account"] == "work", "a launch that could not say keeps the recorded account"
    assert row["last_output_file"] == "/tmp/r2"


async def test_set_state_writes_the_park_reason_to_the_aegis_row(db_pool, _clean):
    assert not await svc.set_state(db_pool, _TASK, status="parked", summary="x"), "no row yet"
    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    assert await svc.set_state(db_pool, _TASK, status="parked", summary="waiting on you: plan")
    row = await svc.get_session(db_pool, _TASK)
    assert row["status"] == "parked" and row["summary"] == "waiting on you: plan"
    with pytest.raises(ValueError):
        await svc.set_state(db_pool, _TASK, status="bogus", summary="")


async def test_create_session_inherits_the_tasks_problem(db_pool, _clean):
    from aegis.services.hub import Event, ingest_event
    from aegis.services.hub_project import link_task

    r = await ingest_event(
        db_pool,
        Event(source="heartbeat", external_id=f"ws-{uuid.uuid4().hex[:8]}", kind="occurrence",
              title="down", klass="DockerServiceDown", subject=f"svc_{uuid.uuid4().hex[:6]}"),
    )
    await link_task(db_pool, r.problem_id, _TASK)
    row = await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    assert row["problem_id"] == r.problem_id


async def test_a_done_aegis_row_makes_room_for_a_fresh_session(db_pool, _clean):
    """The unique index is partial: a `done` AEGIS row is history, and the next
    `create_session` mints a new conversation rather than resuming a finished one."""
    first = await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    await svc.set_state(db_pool, _TASK, status="done", summary="shipped")
    assert await svc.get_session(db_pool, _TASK) is None
    second = await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    assert second["session_id"] != first["session_id"]
    assert [r["status"] for r in await svc.list_for_task(db_pool, _TASK)] == ["done", "active"]


async def test_operator_rows_upsert_per_account_and_never_shadow_the_aegis_row(db_pool, _clean):
    aegis = await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    a = await svc.upsert_operator_session(
        db_pool, task_id=_TASK, account="personal", status="active", summary="looking", session_id="not-a-uuid"
    )
    assert a["owner"] == "operator" and a["agent_id"] == "" and a["account"] == "personal"
    uuid.UUID(a["session_id"])  # minted when the reporter could not say
    b = await svc.upsert_operator_session(
        db_pool, task_id=_TASK, account="personal", status="parked", summary="lunch",
        session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", host="meem",
    )
    assert b["id"] == a["id"] and b["status"] == "parked" and b["summary"] == "lunch"
    assert b["session_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" and b["host"] == "meem"
    c = await svc.upsert_operator_session(db_pool, task_id=_TASK, account="work", status="active", summary="w")
    assert c["id"] not in {a["id"], aegis["id"]}
    assert (await svc.get_session(db_pool, _TASK))["id"] == aegis["id"], "the AEGIS row is untouched"
    assert [r["owner"] for r in await svc.list_for_task(db_pool, _TASK)] == ["aegis", "operator", "operator"]
    assert await svc.find_turns_due(db_pool) == [], "operator rows never become turns"
    # A done row is history: the next report from that account starts a new one.
    await svc.upsert_operator_session(db_pool, task_id=_TASK, account="work", status="done", summary="d")
    d = await svc.upsert_operator_session(db_pool, task_id=_TASK, account="work", status="active", summary="again")
    assert d["id"] != c["id"]
    with pytest.raises(ValueError):
        await svc.upsert_operator_session(db_pool, task_id=_TASK, account="work", status="odd", summary="")


async def test_live_operator_sessions_is_bounded_by_status_and_window(db_pool, _clean):
    await svc.upsert_operator_session(db_pool, task_id=_TASK, account="personal", status="active", summary="s")
    assert [r["account"] for r in await svc.live_operator_sessions(db_pool, _TASK)] == ["personal"]
    await db_pool.execute(
        "UPDATE work_sessions SET last_seen_at = now() - interval '31 minutes' WHERE task_id = $1", _TASK
    )
    assert await svc.live_operator_sessions(db_pool, _TASK) == []
    assert len(await svc.live_operator_sessions(db_pool, _TASK, within=timedelta(hours=1))) == 1
    await svc.upsert_operator_session(db_pool, task_id=_TASK, account="personal", status="parked", summary="s")
    assert await svc.live_operator_sessions(db_pool, _TASK, within=timedelta(hours=1)) == []


async def test_reconcile_touches_listed_rows_and_parks_quiet_unlisted_ones(db_pool, _clean):
    live = await svc.upsert_operator_session(
        db_pool, task_id=_TASK, account="personal", status="active", summary="s",
        session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    )
    gone = await svc.upsert_operator_session(db_pool, task_id=_TASK, account="work", status="active", summary="s")
    fresh = await svc.upsert_operator_session(db_pool, task_id=_TASK, account="other", status="active", summary="s")
    await db_pool.execute(
        "UPDATE work_sessions SET last_seen_at = now() - interval '2 hours' WHERE id = ANY($1::uuid[])",
        [uuid.UUID(live["id"]), uuid.UUID(gone["id"])],
    )
    out = await svc.reconcile_operator_sessions(db_pool, [live["session_id"], "zzz"])
    assert out == {"refreshed": 1, "parked": 1}
    status = {r["account"]: r["status"] for r in await svc.list_for_task(db_pool, _TASK)}
    assert status == {"personal": "active", "work": "parked", "other": "active"}
    assert fresh["status"] == "active"
    assert await svc.reconcile_operator_sessions(db_pool, []) == {"refreshed": 0, "parked": 0}
