"""Agent-comment-channel branch tests for ClarifyActivities."""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis.clarify_note import AGENT_REPLY_PREFIX
from aegis_worker.activities.clarify import ClarifyActivities


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def _auto_content_route(seed_app_route):
    """Every test here classifies APP-<n>: tasks via the seeded content route
    (config-driven replacement for the old hardcoded ^APP-\\d+: pattern)."""
    yield


@pytest_asyncio.fixture(loop_scope="function")
async def _inbox_seeded(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {
                "inbox": "PROJ-INBOX",
                "projects": "PROJ-PRJ",
                "single_actions": "PROJ-SA",
            },
        )
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('PROJ-INBOX', 'Inbox', true, '{}'::jsonb), "
            "       ('PROJ-PRJ', 'Projects', true, '{}'::jsonb), "
            "       ('PROJ-SA', 'Single Actions', true, '{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING"
        )


@pytest.mark.asyncio
async def test_find_unclassified_excludes_agent_reply_prefix(db_pool, _inbox_seeded):
    """A note starting with AGENT_REPLY_PREFIX must NOT be picked up as
    latest_user_note — otherwise the agent's own reply re-triggers itself.
    """
    async with db_pool.acquire() as conn:
        # Clean up from any prior run
        await conn.execute("DELETE FROM todoist_notes WHERE item_id = 'task-aag'")
        await conn.execute("DELETE FROM todoist_tasks WHERE id = 'task-aag'")
        # Insert a task whose last_note_at > last_clarified_at so it would
        # re-emerge if any note qualified as user-authored.
        await conn.execute(
            "INSERT INTO todoist_tasks (id, content, project_id, source_tag, "
            "labels, last_note_at, last_clarified_at, raw) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, '{}'::jsonb)",
            "task-aag",
            "Spike: Tigris vs S3",
            "PROJ-INBOX",
            "#manual",
            ["@raphael"],
            dt.datetime(2026, 5, 26, 12, 5, tzinfo=dt.UTC),
            dt.datetime(2026, 5, 26, 11, 0, tzinfo=dt.UTC),
        )
        # An agent reply (NOT user comment) — must be skipped by extraction.
        await conn.execute(
            "INSERT INTO todoist_notes (id, item_id, content, posted_at) VALUES ($1, $2, $3, $4)",
            "note-1",
            "task-aag",
            f"{AGENT_REPLY_PREFIX}12:30 agent=raphael]\nReply text here.",
            dt.datetime(2026, 5, 26, 12, 30, tzinfo=dt.UTC),
        )
        # An older user comment that SHOULD be picked up.
        await conn.execute(
            "INSERT INTO todoist_notes (id, item_id, content, posted_at) VALUES ($1, $2, $3, $4)",
            "note-2",
            "task-aag",
            "Tell me what we know.",
            dt.datetime(2026, 5, 26, 12, 5, tzinfo=dt.UTC),
        )

    acts = ClarifyActivities(db_pool=db_pool)
    rows = await acts.find_unclassified_items(max_items=10)

    assert len(rows) == 1
    assert rows[0]["latest_user_note"] == "Tell me what we know."


@pytest.mark.asyncio
async def test_find_unclassified_does_not_loop_on_agent_reply_note(db_pool, _inbox_seeded):
    """Regression for the loop bug caught in prod 2026-05-27 — three
    agent-addressed tasks were re-classified on every 15-min tick because
    AgentChatReplyFlow's own success/error reply notes bumped
    last_note_at past last_clarified_at, satisfying the raw
    `last_note_at > last_clarified_at` eligibility filter even though
    no new USER comment had been posted.

    Setup: last_clarified_at AFTER the user comment, then an
    agent-reply note posted LATER bumps last_note_at. The fixed
    eligibility query compares against the max posted_at of
    user-authored notes only (mirroring the latest_user_note
    AEGIS-author exclusion), so this task must NOT come back.
    """
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM todoist_notes WHERE item_id = 'task-loop'")
        await conn.execute("DELETE FROM todoist_tasks WHERE id = 'task-loop'")
        await conn.execute(
            "INSERT INTO todoist_tasks (id, content, project_id, source_tag, "
            "labels, last_note_at, last_clarified_at, raw) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, '{}'::jsonb)",
            "task-loop",
            "Investigate something",
            "PROJ-INBOX",
            "#manual",
            ["@maou"],
            # last_note_at = agent reply timestamp (the loop driver)
            dt.datetime(2026, 5, 27, 18, 30, tzinfo=dt.UTC),
            # last_clarified_at is AFTER the original user comment
            dt.datetime(2026, 5, 27, 18, 15, tzinfo=dt.UTC),
        )
        # User comment posted BEFORE the prior clarify ran
        await conn.execute(
            "INSERT INTO todoist_notes (id, item_id, content, posted_at) VALUES ($1, $2, $3, $4)",
            "note-loop-user",
            "task-loop",
            "Look into this @maou",
            dt.datetime(2026, 5, 27, 9, 40, tzinfo=dt.UTC),
        )
        # Agent reply posted AFTER the last clarify — bumps last_note_at
        # but must NOT make the task re-eligible (loop bug fix).
        await conn.execute(
            "INSERT INTO todoist_notes (id, item_id, content, posted_at) VALUES ($1, $2, $3, $4)",
            "note-loop-agent",
            "task-loop",
            f"{AGENT_REPLY_PREFIX}18:30 UTC agent=maou]\nMy reply.",
            dt.datetime(2026, 5, 27, 18, 30, tzinfo=dt.UTC),
        )

    try:
        acts = ClarifyActivities(db_pool=db_pool)
        rows = await acts.find_unclassified_items(max_items=10)

        assert not any(r["id"] == "task-loop" for r in rows), (
            "Loop bug regression: agent-reply note bumped last_note_at past "
            "last_clarified_at, but eligibility filter must use the latest "
            "USER-authored note's posted_at to avoid the 15-min reply loop."
        )
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM todoist_notes WHERE item_id = 'task-loop'")
            await conn.execute("DELETE FROM todoist_tasks WHERE id = 'task-loop'")


@pytest.mark.asyncio
async def test_find_unclassified_picks_up_genuine_new_user_comment(db_pool, _inbox_seeded):
    """Counterpart to the loop-regression: a genuine NEW user comment
    posted AFTER last_clarified_at must still re-eligible the task.
    Pins the positive case so a future tightening of the filter doesn't
    accidentally block legitimate follow-ups.
    """
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM todoist_notes WHERE item_id = 'task-newcom'")
        await conn.execute("DELETE FROM todoist_tasks WHERE id = 'task-newcom'")
        await conn.execute(
            "INSERT INTO todoist_tasks (id, content, project_id, source_tag, "
            "labels, last_note_at, last_clarified_at, raw) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, '{}'::jsonb)",
            "task-newcom",
            "Some task",
            "PROJ-INBOX",
            "#manual",
            ["@maou"],
            dt.datetime(2026, 5, 27, 18, 30, tzinfo=dt.UTC),
            dt.datetime(2026, 5, 27, 18, 15, tzinfo=dt.UTC),
        )
        # Agent reply (old, before clarify): doesn't re-eligible
        await conn.execute(
            "INSERT INTO todoist_notes (id, item_id, content, posted_at) VALUES ($1, $2, $3, $4)",
            "note-newcom-agent",
            "task-newcom",
            f"{AGENT_REPLY_PREFIX}18:00 UTC agent=maou]\nOld reply.",
            dt.datetime(2026, 5, 27, 18, 0, tzinfo=dt.UTC),
        )
        # NEW user comment AFTER last_clarified_at — task MUST re-eligible
        await conn.execute(
            "INSERT INTO todoist_notes (id, item_id, content, posted_at) VALUES ($1, $2, $3, $4)",
            "note-newcom-user",
            "task-newcom",
            "Actually, can you try this other angle?",
            dt.datetime(2026, 5, 27, 18, 30, tzinfo=dt.UTC),
        )

    try:
        acts = ClarifyActivities(db_pool=db_pool)
        rows = await acts.find_unclassified_items(max_items=10)

        matched = [r for r in rows if r["id"] == "task-newcom"]
        assert len(matched) == 1
        assert matched[0]["latest_user_note"] == "Actually, can you try this other angle?"
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM todoist_notes WHERE item_id = 'task-newcom'")
            await conn.execute("DELETE FROM todoist_tasks WHERE id = 'task-newcom'")


@pytest.mark.parametrize(
    "label, expected_classification, expected_assignee",
    [
        ("@sebas", "sebas_followup", "@sebas"),
        ("@raphael", "raphael_followup", "@raphael"),
        ("@maou", "maou_followup", "@maou"),
    ],
)
@pytest.mark.asyncio
async def test_classify_one_short_circuits_on_addressable_agent_with_fresh_comment(
    db_pool, label, expected_classification, expected_assignee
):
    """When the user puts @<agent> on a task AND there's a fresh comment,
    classify_one MUST return the per-agent followup classification WITHOUT
    calling the LLM (llm_model='rules').
    """
    from unittest.mock import AsyncMock

    llm = AsyncMock()
    acts = ClarifyActivities(db_pool=db_pool, llm_client=llm)
    task = {
        "id": "task-x",
        "content": "Spike: cold storage options",
        "description": "",
        "labels": [label, "#manual"],
        "source_tag": "#manual",
        "latest_user_note": "Tell me what we know.",
    }

    decision = await acts.classify_one(task)

    assert decision["classification"] == expected_classification
    assert decision["assignee"] == expected_assignee
    assert decision["llm_model"] == "rules"
    llm.think.assert_not_called()


@pytest.mark.asyncio
async def test_classify_one_no_short_circuit_when_user_note_empty(db_pool):
    """@sebas alone (no fresh comment) must NOT short-circuit — that lets
    classify_one fall through to the LLM.
    """
    from unittest.mock import AsyncMock

    llm = AsyncMock()
    # think returns a dict in the production shape
    llm.think.return_value = {
        "response": '{"classification":"next_action","confidence":0.9,'
        '"assignee":"@me","contexts":["@deep"],"reason":"r"}',
        "prompt_tokens": 10,
        "completion_tokens": 5,
    }
    acts = ClarifyActivities(db_pool=db_pool, llm_client=llm)
    task = {
        "id": "task-x",
        "content": "Pick a storage",
        "description": "",
        "labels": ["@sebas", "#manual"],
        "source_tag": "#manual",
        "latest_user_note": None,
    }

    decision = await acts.classify_one(task)

    # Should have called LLM — no short-circuit on empty note
    assert decision["llm_model"] != "rules"
    llm.think.assert_called()


@pytest.mark.asyncio
async def test_classify_one_first_match_wins_sebas_then_raphael(db_pool):
    """Documented iteration order: @sebas → @raphael → @maou. If both
    @sebas and @raphael are present, sebas wins.
    """
    from unittest.mock import AsyncMock

    acts = ClarifyActivities(db_pool=db_pool, llm_client=AsyncMock())
    task = {
        "id": "task-x",
        "content": "Research X",
        "description": "",
        "labels": ["@raphael", "@sebas"],
        "source_tag": "#manual",
        "latest_user_note": "What's our take?",
    }

    decision = await acts.classify_one(task)

    assert decision["classification"] == "sebas_followup"


@pytest.mark.asyncio
async def test_post_agent_reply_comment_uses_prefix_and_tool_trailer(db_pool):
    """post_agent_reply_comment writes a note that:
    - starts with AGENT_REPLY_PREFIX (so self-loop guards trip)
    - includes agent=<id> in the prefix block
    - appends a `(tools: ...)` trailer when a non-empty tool summary is given
    """
    todoist = AsyncMock()
    todoist.commands = AsyncMock(return_value={"ok": True, "sync_status": {}})

    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=todoist)
    # Monkeypatch check_sync_status to declare ok:
    from aegis.connectors.todoist import TodoistConnector

    real_check = TodoistConnector.check_sync_status
    TodoistConnector.check_sync_status = staticmethod(
        lambda r, u: {
            "ok": True,
            "envelope_error": None,
            "retryable": False,
            "rejected": {},
            "rejected_retryable": False,
        }
    )
    try:
        result = await acts.post_agent_reply_comment(
            task_id="task-x",
            agent_id="raphael",
            reply_text="Tigris is an S3-compatible store.",
            tool_trace_summary="search_knowledge",
            message_id=987,
        )
    finally:
        TodoistConnector.check_sync_status = real_check

    assert result["posted"] is True
    cmds = todoist.commands.call_args.args[0]
    assert len(cmds) == 1
    note_cmd = cmds[0]
    assert note_cmd["type"] == "note_add"
    content = note_cmd["args"]["content"]
    assert content.startswith(AGENT_REPLY_PREFIX)
    assert "agent=raphael" in content
    assert "Tigris is an S3-compatible store." in content
    assert "(tools: search_knowledge)" in content


@pytest.mark.asyncio
async def test_post_agent_reply_comment_outbox_on_retryable_failure(db_pool):
    """Retryable Todoist failure → outbox queue (existing pattern)."""
    todoist = AsyncMock()
    todoist.commands = AsyncMock(return_value={"ok": False})

    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=todoist)
    from aegis.connectors.todoist import TodoistConnector

    real_check = TodoistConnector.check_sync_status
    TodoistConnector.check_sync_status = staticmethod(
        lambda r, u: {
            "ok": False,
            "envelope_error": "503",
            "retryable": True,
            "rejected": {},
            "rejected_retryable": False,
        }
    )
    try:
        result = await acts.post_agent_reply_comment(
            task_id="task-x",
            agent_id="sebas",
            reply_text="ok",
            tool_trace_summary="",
            message_id=None,
        )
    finally:
        TodoistConnector.check_sync_status = real_check

    assert result["posted"] is False
    assert result["outbox_queued"] >= 1


@pytest.mark.asyncio
async def test_post_agent_reply_error_comment_also_uses_prefix(db_pool):
    """Error comment must ALSO start with AGENT_REPLY_PREFIX so it doesn't
    self-loop the error reply through the agent again.
    """
    todoist = AsyncMock()
    todoist.commands = AsyncMock(return_value={"ok": True, "sync_status": {}})

    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=todoist)
    from aegis.connectors.todoist import TodoistConnector

    real_check = TodoistConnector.check_sync_status
    TodoistConnector.check_sync_status = staticmethod(
        lambda r, u: {
            "ok": True,
            "envelope_error": None,
            "retryable": False,
            "rejected": {},
            "rejected_retryable": False,
        }
    )
    try:
        await acts.post_agent_reply_error_comment(
            task_id="task-x",
            agent_id="raphael",
            reason="LLM proxy timeout",
        )
    finally:
        TodoistConnector.check_sync_status = real_check

    cmds = todoist.commands.call_args.args[0]
    content = cmds[0]["args"]["content"]
    assert content.startswith(AGENT_REPLY_PREFIX)
    assert "agent=raphael" in content
    assert "LLM proxy timeout" in content


@pytest.mark.asyncio
async def test_post_agent_reply_comment_includes_message_id_anchor(db_pool):
    """When message_id is provided, the Todoist comment includes
    a `(chat message_id=<id>)` anchor line so the user can cross-link
    from the Todoist comment to the rich chat reply.
    """
    todoist = AsyncMock()
    todoist.commands = AsyncMock(return_value={"ok": True, "sync_status": {}})

    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=todoist)
    from aegis.connectors.todoist import TodoistConnector

    real_check = TodoistConnector.check_sync_status
    TodoistConnector.check_sync_status = staticmethod(
        lambda r, u: {
            "ok": True,
            "envelope_error": None,
            "retryable": False,
            "rejected": {},
            "rejected_retryable": False,
        }
    )
    try:
        await acts.post_agent_reply_comment(
            task_id="task-x",
            agent_id="raphael",
            reply_text="Reply body",
            tool_trace_summary="",
            message_id=12345,
        )
    finally:
        TodoistConnector.check_sync_status = real_check

    content = todoist.commands.call_args.args[0][0]["args"]["content"]
    assert "(chat message_id=12345)" in content


@pytest.mark.asyncio
async def test_clear_clarify_watermark_sets_null(db_pool, _inbox_seeded):
    """Compensating action when AgentChatReplyFlow spawn fails — clear
    last_clarified_at so the task re-emerges on the next tick.
    """
    import datetime as _dt

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM todoist_tasks WHERE id = 'task-cw'")
        await conn.execute(
            "INSERT INTO todoist_tasks (id, content, project_id, "
            "last_clarified_at, last_note_at) VALUES ($1, $2, $3, $4, $5)",
            "task-cw",
            "x",
            "PROJ-INBOX",
            _dt.datetime(2026, 5, 26, 13, 0, tzinfo=_dt.UTC),
            _dt.datetime(2026, 5, 26, 12, 0, tzinfo=_dt.UTC),
        )

    acts = ClarifyActivities(db_pool=db_pool)
    result = await acts.clear_clarify_watermark(task_id="task-cw")

    assert result["cleared"] is True
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_clarified_at FROM todoist_tasks WHERE id = $1", "task-cw"
        )
    assert row["last_clarified_at"] is None
