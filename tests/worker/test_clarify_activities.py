"""ClarifyActivities — unit tests with mocked LLM/connector."""

from __future__ import annotations

import datetime as dt
import json
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis_worker.activities.clarify import ClarifyActivities


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def _auto_content_route(seed_app_route):
    """Every test here classifies APP-<n>: tasks via the seeded content route
    (config-driven replacement for the old hardcoded ^APP-\\d+: pattern)."""
    yield


@pytest_asyncio.fixture(loop_scope="function")
async def _inbox_seeded(db_pool):
    """Seed inbox-project setting + one source-tagged task."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {
                "inbox": "P_INBOX",
                "projects": "P_PRJ",
                "single_actions": "P_SA",
            },
        )
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('P_INBOX','Inbox',true,'{}'::jsonb), "
            "      ('P_PRJ','Projects',true,'{}'::jsonb), "
            "      ('P_SA','Single Actions',true,'{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING"
        )
        # Reset state for any prior T_NEW row from previous runs
        await conn.execute("DELETE FROM todoist_notes WHERE item_id='T_NEW'")
        await conn.execute("DELETE FROM todoist_tasks WHERE id='T_NEW'")
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, labels, source_tag, is_completed, raw) "
            "VALUES ('T_NEW','P_INBOX','hello',ARRAY['#email'],'#email',false,'{}'::jsonb)"
        )


@pytest.mark.asyncio
async def test_find_unclassified_returns_new_task(db_pool, _inbox_seeded) -> None:
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock())
    rows = await acts.find_unclassified_items(max_items=10)
    assert any(r["id"] == "T_NEW" for r in rows)


@pytest.mark.asyncio
async def test_find_unclassified_skips_completed(db_pool, _inbox_seeded) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE todoist_tasks SET is_completed=true WHERE id='T_NEW'")
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock())
    rows = await acts.find_unclassified_items(max_items=10)
    assert all(r["id"] != "T_NEW" for r in rows)


@pytest.mark.asyncio
async def test_find_unclassified_skips_already_clarified(db_pool, _inbox_seeded) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE todoist_tasks SET last_clarified_at=now() WHERE id='T_NEW'")
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock())
    rows = await acts.find_unclassified_items(max_items=10)
    assert all(r["id"] != "T_NEW" for r in rows)


@pytest.mark.asyncio
async def test_find_unclassified_includes_user_created_agent_addressed_task(
    db_pool, _inbox_seeded
) -> None:
    """User-created Todoist tasks (no source_tag, not APP-Jira) labelled
    @sebas/@raphael/@maou/@pandora must be eligible for clarify so the
    comment-channel agent-followup short-circuit can run.

    Regression: prod task `6ghxpGJ7q2j5P6mv` (labelled @raphael, user
    note posted, last_clarified_at=NULL) was silently dropped because
    the SQL eligibility filter required source_tag IS NOT NULL OR
    APP-N: prefix. Found 2026-05-26 during post-deploy audit of the
    comment-channel feature.
    """
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM todoist_notes WHERE item_id='T_USER_RAPHAEL'")
        await conn.execute("DELETE FROM todoist_tasks WHERE id='T_USER_RAPHAEL'")
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, labels, source_tag, is_completed, raw) "
            "VALUES ('T_USER_RAPHAEL','P_INBOX','do investigation about X',"
            "ARRAY['@raphael'], NULL, false, '{}'::jsonb)"
        )
        await conn.execute(
            "INSERT INTO todoist_notes (id, item_id, content, posted_at) "
            "VALUES ('N_USER_RAPHAEL','T_USER_RAPHAEL','find out and tell me details',"
            "now() - interval '5 minutes')"
        )
        await conn.execute(
            "UPDATE todoist_tasks SET last_note_at = now() - interval '5 minutes' "
            "WHERE id='T_USER_RAPHAEL'"
        )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock())
    rows = await acts.find_unclassified_items(max_items=20)
    matching = next((r for r in rows if r["id"] == "T_USER_RAPHAEL"), None)
    assert matching is not None, (
        "user-created @raphael-labelled task with no source_tag must be eligible "
        "for clarify; today's filter drops it and the comment-channel never fires"
    )
    assert matching["latest_user_note"] == "find out and tell me details"


@pytest.mark.asyncio
async def test_find_unclassified_still_skips_user_tasks_without_agent_label(
    db_pool, _inbox_seeded
) -> None:
    """Negative companion: a user-created task with NO source_tag, NO
    APP- prefix, and NO addressable-agent label must STILL be excluded.
    The eligibility relaxation is scoped to addressable-agent labels —
    not a general "all Inbox tasks are eligible" change.
    """
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM todoist_tasks WHERE id='T_USER_ORPHAN'")
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, labels, source_tag, is_completed, raw) "
            "VALUES ('T_USER_ORPHAN','P_INBOX','random thought',"
            "ARRAY['@misc'], NULL, false, '{}'::jsonb)"
        )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock())
    rows = await acts.find_unclassified_items(max_items=20)
    assert all(r["id"] != "T_USER_ORPHAN" for r in rows), (
        "task with no source_tag, no APP- prefix, and no addressable-agent "
        "label must remain excluded — eligibility relaxation must be scoped"
    )


@pytest_asyncio.fixture(loop_scope="function")
async def _chat_task_seeded(db_pool, _inbox_seeded):
    """A user-created comment-channel task labelled @sebas (chat-followup)."""
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM todoist_notes WHERE item_id='T_CHAT'")
        await conn.execute("DELETE FROM todoist_tasks WHERE id='T_CHAT'")
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, labels, source_tag, is_completed, raw) "
            "VALUES ('T_CHAT','P_INBOX','ping sebas',ARRAY['@sebas'],NULL,false,'{}'::jsonb)"
        )
    yield
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM todoist_notes WHERE item_id='T_CHAT'")
        await conn.execute("DELETE FROM todoist_tasks WHERE id='T_CHAT'")


@pytest.mark.asyncio
async def test_find_unclassified_chat_reply_error_cooldown_suppresses(
    db_pool, _chat_task_seeded
) -> None:
    """A recent `[Agent reply @ ... ERROR]` note with no newer user note must
    suppress the task for the cooldown window — otherwise AgentChatReplyFlow
    re-spawns and re-errors every tick (the 2026-05-30 loop pattern)."""
    now = dt.datetime.now(dt.UTC)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_notes (id, item_id, content, posted_at) VALUES "
            "('NC_USER', 'T_CHAT', 'hey sebas', $1),"
            "('NC_ERR', 'T_CHAT', '[Agent reply @ 09:00 UTC agent=sebas ERROR]\nDropped the reply: boom', $2)"
            "ON CONFLICT DO NOTHING",
            now - dt.timedelta(minutes=10),
            now - dt.timedelta(minutes=5),
        )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock())
    rows = await acts.find_unclassified_items(max_items=20)
    assert all(r["id"] != "T_CHAT" for r in rows), "errored reply must be on cooldown"


@pytest.mark.asyncio
async def test_find_unclassified_chat_reply_error_cooldown_lifts_on_new_user_note(
    db_pool, _chat_task_seeded
) -> None:
    """A user comment AFTER the errored reply lifts the cooldown — genuine new
    input must be processed, not blocked."""
    now = dt.datetime.now(dt.UTC)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_notes (id, item_id, content, posted_at) VALUES "
            "('NC_ERR2', 'T_CHAT', '[Agent reply @ 09:00 UTC agent=sebas ERROR]\nDropped the reply: boom', $1),"
            "('NC_USER2', 'T_CHAT', 'try again please', $2)"
            "ON CONFLICT DO NOTHING",
            now - dt.timedelta(minutes=5),
            now - dt.timedelta(minutes=2),
        )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock())
    rows = await acts.find_unclassified_items(max_items=20)
    matching = next((r for r in rows if r["id"] == "T_CHAT"), None)
    assert matching is not None, "a new user note after the error must re-eligible the task"
    assert matching["latest_user_note"] == "try again please"


@pytest.mark.asyncio
async def test_find_unclassified_requeues_after_user_note(db_pool, _inbox_seeded) -> None:
    """When user adds a comment AFTER last_clarified_at, task re-emerges."""
    earlier = dt.datetime(2026, 5, 19, 10, 0, 0, tzinfo=dt.UTC)
    later = dt.datetime(2026, 5, 19, 12, 0, 0, tzinfo=dt.UTC)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE todoist_tasks SET last_clarified_at=$1, last_note_at=$2 WHERE id='T_NEW'",
            earlier,
            later,
        )
        await conn.execute(
            "INSERT INTO todoist_notes (id, item_id, content, posted_at) "
            "VALUES ('N1', 'T_NEW', 'this is reference not email', $1) "
            "ON CONFLICT DO NOTHING",
            later,
        )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock())
    rows = await acts.find_unclassified_items(max_items=10)
    matching = next((r for r in rows if r["id"] == "T_NEW"), None)
    assert matching is not None
    assert matching["latest_user_note"] == "this is reference not email"


@pytest.mark.asyncio
async def test_find_unclassified_latest_user_note_skips_aegis_workflow_run_notes(
    db_pool, _inbox_seeded
) -> None:
    """latest_user_note subquery must skip notes that contain the
    `Workflow run:` footer (AEGIS-authored). Otherwise Pandora's own
    progress comments are mistaken for user input and re-trigger
    pandora_followup every clarify tick."""
    earlier = dt.datetime(2026, 5, 19, 10, 0, 0, tzinfo=dt.UTC)
    aegis_posted = dt.datetime(2026, 5, 19, 11, 0, 0, tzinfo=dt.UTC)
    user_posted = dt.datetime(2026, 5, 19, 11, 30, 0, tzinfo=dt.UTC)
    later_aegis = dt.datetime(2026, 5, 19, 12, 0, 0, tzinfo=dt.UTC)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE todoist_tasks SET last_clarified_at=$1, last_note_at=$2 WHERE id='T_NEW'",
            earlier,
            later_aegis,
        )
        # Three notes — one user, two AEGIS-authored at various points.
        await conn.execute(
            "INSERT INTO todoist_notes (id, item_id, content, posted_at) VALUES "
            "('N_USER_NOTE', 'T_NEW', 'genuine user comment', $1),"
            "('N_AEGIS_EARLY', 'T_NEW', '🎭 scoping started\nWorkflow run: x', $2),"
            "('N_AEGIS_LATE', 'T_NEW', '🎭 scoping complete\nWorkflow run: x', $3)"
            "ON CONFLICT DO NOTHING",
            user_posted,
            aegis_posted,
            later_aegis,
        )
    try:
        acts = ClarifyActivities(
            db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock()
        )
        rows = await acts.find_unclassified_items(max_items=10)
        matching = next((r for r in rows if r["id"] == "T_NEW"), None)
        assert matching is not None
        # latest_user_note must be the user comment, not the later AEGIS note.
        assert matching["latest_user_note"] == "genuine user comment"
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM todoist_notes WHERE id IN "
                "('N_USER_NOTE', 'N_AEGIS_EARLY', 'N_AEGIS_LATE')"
            )


@pytest.mark.asyncio
async def test_find_unclassified_skips_task_with_only_aegis_notes_after_watermark(
    db_pool, _inbox_seeded
) -> None:
    """Eligibility regression (2026-05-27 loop fix): a task whose only
    post-watermark notes are AEGIS-authored (Workflow run: footer, agent
    reply prefix, ClarifyFlow prefix) must NOT be surfaced. Pre-fix, any
    note bumped last_note_at and re-eligibled the task — pandora's own
    reply notes drove a 15-min self-trigger loop on 3 prod tasks.
    """
    earlier = dt.datetime(2026, 5, 19, 10, 0, 0, tzinfo=dt.UTC)
    aegis_posted = dt.datetime(2026, 5, 19, 12, 0, 0, tzinfo=dt.UTC)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE todoist_tasks SET last_clarified_at=$1, last_note_at=$2 WHERE id='T_NEW'",
            earlier,
            aegis_posted,
        )
        await conn.execute(
            "INSERT INTO todoist_notes (id, item_id, content, posted_at) VALUES "
            "('N_AEGIS_ONLY', 'T_NEW', '🎭 verdict\nWorkflow run: y', $1) "
            "ON CONFLICT DO NOTHING",
            aegis_posted,
        )
    try:
        acts = ClarifyActivities(
            db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock()
        )
        rows = await acts.find_unclassified_items(max_items=10)
        matching = next((r for r in rows if r["id"] == "T_NEW"), None)
        assert matching is None, (
            "Loop regression: AEGIS-only notes posted after last_clarified_at "
            "must NOT re-eligible the task. Filter on user-authored notes only."
        )
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM todoist_notes WHERE id = 'N_AEGIS_ONLY'")


@pytest.mark.asyncio
async def test_find_unclassified_pandora_cooldown_suppresses_within_30min(
    db_pool, _inbox_seeded
) -> None:
    """Pandora cooldown (defence-in-depth): a @pandora APP- task that
    had an AlertInvestigationFlow start within the last 30 min is NOT
    re-surfaced, regardless of last_note_at vs last_clarified_at. This
    blocks any runaway loop that bypasses the `Workflow run:` filter."""
    task_id = "T_PANDORA_COOLDOWN"
    earlier = dt.datetime(2026, 5, 22, 10, 0, 0, tzinfo=dt.UTC)
    fresh_note = dt.datetime(2026, 5, 22, 10, 30, 0, tzinfo=dt.UTC)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, labels, is_completed, raw, "
            " last_clarified_at, last_note_at) "
            "VALUES ($1,'P_INBOX','APP-99999: cooldown probe',"
            " ARRAY['@pandora','@area/acme'],false,'{}'::jsonb,$2,$3) "
            "ON CONFLICT (id) DO UPDATE SET "
            "  labels = EXCLUDED.labels, "
            "  last_clarified_at = EXCLUDED.last_clarified_at, "
            "  last_note_at = EXCLUDED.last_note_at",
            task_id,
            earlier,
            fresh_note,
        )
        # Seed a workflow_runs row 10 min ago — well inside the cooldown.
        await conn.execute(
            """
            INSERT INTO workflow_runs
              (run_id, workflow_id, workflow_type, agent_id, status,
               started_at, completed_at, duration_ms)
            VALUES ($1, $2, 'AlertInvestigationFlow', 'pandoras-actor', 'completed',
                    NOW() - INTERVAL '10 minutes', NOW() - INTERVAL '5 minutes', 300000)
            ON CONFLICT (run_id) DO NOTHING
            """,
            f"run-{task_id}-recent",
            f"pandora-jira-{task_id}-recent",
        )
    try:
        acts = ClarifyActivities(
            db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock()
        )
        rows = await acts.find_unclassified_items(max_items=20)
        ids = [r["id"] for r in rows]
        assert task_id not in ids, f"Cooldown should have suppressed {task_id}, got {ids}"
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM workflow_runs WHERE run_id = $1",
                f"run-{task_id}-recent",
            )
            await conn.execute("DELETE FROM todoist_tasks WHERE id = $1", task_id)


@pytest.mark.asyncio
async def test_find_unclassified_pandora_cooldown_lifts_after_30min(db_pool, _inbox_seeded) -> None:
    """After 30 min since the last investigation, the cooldown lifts —
    a @pandora APP- task with a fresh USER comment past last_clarified_at
    surfaces normally. The user-note is required (2026-05-27 loop fix):
    bare last_note_at advance from an agent reply does NOT surface."""
    task_id = "T_PANDORA_COOLDOWN_LIFT"
    earlier = dt.datetime(2026, 5, 22, 8, 0, 0, tzinfo=dt.UTC)
    fresh_note = dt.datetime(2026, 5, 22, 9, 0, 0, tzinfo=dt.UTC)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, labels, is_completed, raw, "
            " last_clarified_at, last_note_at) "
            "VALUES ($1,'P_INBOX','APP-77777: cooldown lifted',"
            " ARRAY['@pandora'],false,'{}'::jsonb,$2,$3) "
            "ON CONFLICT (id) DO UPDATE SET "
            "  labels = EXCLUDED.labels, "
            "  last_clarified_at = EXCLUDED.last_clarified_at, "
            "  last_note_at = EXCLUDED.last_note_at",
            task_id,
            earlier,
            fresh_note,
        )
        # Workflow run completed 2 hours ago — well outside cooldown.
        await conn.execute(
            """
            INSERT INTO workflow_runs
              (run_id, workflow_id, workflow_type, agent_id, status,
               started_at, completed_at, duration_ms)
            VALUES ($1, $2, 'AlertInvestigationFlow', 'pandoras-actor', 'completed',
                    NOW() - INTERVAL '2 hours', NOW() - INTERVAL '110 minutes', 600000)
            ON CONFLICT (run_id) DO NOTHING
            """,
            f"run-{task_id}-old",
            f"pandora-jira-{task_id}-old",
        )
        # A user-authored followup note past last_clarified_at — required
        # for the eligibility filter post-2026-05-27 loop fix.
        await conn.execute(
            "INSERT INTO todoist_notes (id, item_id, content, posted_at) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING",
            f"note-{task_id}-user",
            task_id,
            "any update?",
            fresh_note,
        )
    try:
        acts = ClarifyActivities(
            db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock()
        )
        rows = await acts.find_unclassified_items(max_items=20)
        ids = [r["id"] for r in rows]
        assert task_id in ids
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM workflow_runs WHERE run_id = $1",
                f"run-{task_id}-old",
            )
            await conn.execute("DELETE FROM todoist_notes WHERE id = $1", f"note-{task_id}-user")
            await conn.execute("DELETE FROM todoist_tasks WHERE id = $1", task_id)


@pytest.mark.asyncio
async def test_find_unclassified_pandora_cooldown_doesnt_block_first_investigation(
    db_pool, _inbox_seeded
) -> None:
    """A brand-new @pandora APP- task (or any APP- task) with no prior
    AlertInvestigationFlow row must NOT be blocked by the cooldown — the
    first investigation needs to fire."""
    task_id = "T_PANDORA_FIRST"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM workflow_runs WHERE workflow_id LIKE $1", f"pandora-jira-{task_id}-%"
        )
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, labels, is_completed, raw) "
            "VALUES ($1,'P_INBOX','APP-66666: first-time investigation',"
            " ARRAY[]::text[],false,'{}'::jsonb) "
            "ON CONFLICT (id) DO UPDATE SET labels = EXCLUDED.labels, "
            "  last_clarified_at = NULL, last_note_at = NULL",
            task_id,
        )
    try:
        acts = ClarifyActivities(
            db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock()
        )
        rows = await acts.find_unclassified_items(max_items=20)
        ids = [r["id"] for r in rows]
        assert task_id in ids
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM todoist_tasks WHERE id = $1", task_id)


@pytest.mark.asyncio
async def test_find_unclassified_no_routes_admits_no_app_tasks(db_pool, _inbox_seeded) -> None:
    """With NO content routes configured (the OSS default), an APP-<n>: task that
    has no source_tag and no agent label is NOT admitted — and the query doesn't
    crash on the empty `t.content ~ ANY('{}')` filter."""
    from aegis.services.content_routes import save_content_routes
    from aegis_worker.activities import clarify as _cl

    await save_content_routes(db_pool, [])  # clear routes the autouse fixture seeded
    _cl._routes_cache.update(routes=None, ts=0.0)
    task_id = "T_NOROUTE_APP"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, is_completed, raw) "
            "VALUES ($1,'P_INBOX','APP-424242: no route configured', "
            " ARRAY[]::text[], false, '{}'::jsonb) "
            "ON CONFLICT (id) DO UPDATE SET labels=EXCLUDED.labels, last_clarified_at=NULL",
            task_id,
        )
    try:
        acts = ClarifyActivities(
            db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock()
        )
        rows = await acts.find_unclassified_items(max_items=20)
        assert isinstance(rows, list)
        assert task_id not in [r["id"] for r in rows]
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM todoist_tasks WHERE id = $1", task_id)


@pytest.mark.asyncio
async def test_find_unclassified_returns_empty_when_no_inbox_setting(db_pool) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM settings WHERE key='todoist_managed_project_ids'")
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock())
    rows = await acts.find_unclassified_items(max_items=10)
    assert rows == []


@pytest.mark.asyncio
async def test_find_unclassified_returns_empty_when_kill_switch_off(db_pool, _inbox_seeded) -> None:
    """gtd_clarify_enabled=false short-circuits the find — no downstream
    work runs, so the kill switch can't poison last_clarified_at or spam
    NEEDS REVIEW comments on every Inbox task.
    """
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('gtd_clarify_enabled', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            False,
        )
    try:
        acts = ClarifyActivities(
            db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock()
        )
        rows = await acts.find_unclassified_items(max_items=10)
        assert rows == []
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES ('gtd_clarify_enabled', $1) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                True,
            )


def _llm_mock_returning(payload: dict) -> AsyncMock:
    """Make an LLM mock whose .think returns JSON."""
    m = AsyncMock()
    m.think = AsyncMock(return_value=json.dumps(payload))
    return m


@pytest.mark.asyncio
async def test_classify_one_kill_switch_off_returns_skipped(db_pool) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('gtd_clarify_enabled', 'false'::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
    acts = ClarifyActivities(
        db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=_llm_mock_returning({})
    )
    task = {
        "id": "T1",
        "content": "x",
        "source_tag": "#email",
        "labels": ["#email"],
        "description": None,
        "latest_user_note": None,
    }
    result = await acts.classify_one(task)
    assert result["classification"] == "skipped"
    # Reset for following tests
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE settings SET value='true'::jsonb WHERE key='gtd_clarify_enabled'"
        )


@pytest.mark.asyncio
async def test_classify_one_skip_inbox_short_circuits_research(db_pool) -> None:
    """#research bypasses LLM via _RULES.skip_inbox lookup."""
    llm = _llm_mock_returning({"classification": "should-not-be-called"})
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=llm)
    task = {
        "id": "T2",
        "content": "Some HN article",
        "source_tag": "#research",
        "labels": ["#research"],
        "description": None,
        "latest_user_note": None,
    }
    result = await acts.classify_one(task)
    assert result["classification"] == "reference"
    assert result["confidence"] == 1.0
    assert result["llm_model"] == "rules"
    assert result["assignee"] == "@raphael"
    llm.think.assert_not_awaited()


@pytest.mark.asyncio
async def test_classify_one_high_confidence_primary_no_escalation(db_pool) -> None:
    """qwen3:14b >= 0.7 -> no Sonnet call."""
    llm = AsyncMock()
    llm.think = AsyncMock(
        return_value=json.dumps(
            {
                "classification": "next_action",
                "confidence": 0.85,
                "assignee": "@sebas",
                "contexts": ["@email", "@5min"],
                "reason": "reply due",
            }
        )
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=llm)
    task = {
        "id": "T3",
        "content": "Reply to vendor",
        "source_tag": "#email",
        "labels": ["#email"],
        "description": None,
        "latest_user_note": None,
    }
    result = await acts.classify_one(task)
    assert result["classification"] == "next_action"
    assert result["confidence"] == 0.85
    assert result["llm_model"] == "qwen3:14b"
    assert llm.think.await_count == 1


@pytest.mark.asyncio
async def test_classify_one_handles_real_llm_dict_return_shape(db_pool) -> None:
    """LLMClient.think returns dict {response, model, prompt_tokens, completion_tokens}.

    Earlier mocks returned bare strings, which hid a prod break. This
    test asserts the activity can consume the real dict shape.
    """
    llm = AsyncMock()
    llm.think = AsyncMock(
        return_value={
            "response": json.dumps(
                {
                    "classification": "next_action",
                    "confidence": 0.82,
                    "assignee": "@sebas",
                    "contexts": ["@email"],
                    "reason": "vendor reply",
                }
            ),
            "model": "qwen3:14b",
            "prompt_tokens": 130,
            "completion_tokens": 45,
        }
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=llm)
    task = {
        "id": "T_DICT",
        "content": "x",
        "source_tag": "#email",
        "labels": ["#email"],
        "description": None,
        "latest_user_note": None,
    }
    result = await acts.classify_one(task)
    assert result["classification"] == "next_action"
    assert result["confidence"] == 0.82
    assert result["llm_model"] == "qwen3:14b"
    assert result["prompt_tokens"] == 130
    assert result["completion_tokens"] == 45


@pytest.mark.asyncio
async def test_classify_one_low_confidence_no_escalation(db_pool) -> None:
    """Issue #117 removed the dead Sonnet escalation branch. A low-confidence
    qwen3:14b result is now returned verbatim in a single call (no second
    model); the low-confidence card is spawned later by apply_outcome."""
    llm = AsyncMock()
    llm.think = AsyncMock(
        return_value=json.dumps(
            {
                "classification": "someday",
                "confidence": 0.4,
                "assignee": "@me",
                "contexts": ["@reading"],
                "reason": "unclear",
            }
        )
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=llm)
    task = {
        "id": "T4",
        "content": "x",
        "source_tag": "#email",
        "labels": ["#email"],
        "description": None,
        "latest_user_note": None,
    }
    result = await acts.classify_one(task)
    assert result["classification"] == "someday"
    assert result["confidence"] == 0.4
    assert result["llm_model"] == "qwen3:14b"
    assert llm.think.await_count == 1


@pytest.mark.asyncio
async def test_classify_one_user_hint_propagates_into_prompt(db_pool) -> None:
    """latest_user_note flows into the LLM prompt."""
    llm = AsyncMock()
    llm.think = AsyncMock(
        return_value=json.dumps(
            {
                "classification": "reference",
                "confidence": 0.95,
                "assignee": "@raphael",
                "contexts": ["@reading"],
                "reason": "user",
            }
        )
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=llm)
    task = {
        "id": "T5",
        "content": "Apple WWDC notes",
        "source_tag": "#email",
        "labels": ["#email"],
        "description": None,
        "latest_user_note": "not action — just save for later",
    }
    await acts.classify_one(task)
    prompt = llm.think.await_args.args[0]
    assert "not action — just save for later" in prompt
    assert "authoritative supervision" in prompt


def test_looks_like_notification_markers() -> None:
    """Issue #117: notification-shape titles are recognised; genuine
    action-y titles are not."""
    f = ClarifyActivities._looks_like_notification
    # Real trashed 2_min titles from prod gtd_clarify_log.
    assert f("Cloudflare Access login code for truenas.hikmahtech.in")
    assert f("Mohammed Arshad, please verify your new device")
    assert f("Security alert for conspiracyonly@gmail.com")
    assert f("Prema Khan wants to be friends on Facebook")
    assert f("You have 1 new invitation")
    assert f("[GitHub] Your Dependabot alerts for the week of Jul 7 - Jul 14")
    assert f("Welcome to Bluesky")
    # Genuine quick actions must NOT match.
    assert not f("Reply to vendor about the March invoice")
    assert not f("Confirm dinner reservation for Friday")
    assert not f(None)
    assert not f("")


@pytest.mark.asyncio
async def test_classify_one_downgrades_notification_2min_to_trash(db_pool) -> None:
    """Issue #117: an #email task the LLM tags 2_min whose title reads like a
    notification is junk → downgraded to trash so it never reaches a card."""
    llm = AsyncMock()
    llm.think = AsyncMock(
        return_value=json.dumps(
            {
                "classification": "2_min",
                "confidence": 0.95,
                "assignee": "@me",
                "contexts": ["@5min"],
                "reason": "quick",
            }
        )
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=llm)
    task = {
        "id": "T_NOTIF",
        "content": "Cloudflare Access login code for postiz.hikmahtech.in",
        "source_tag": "#email",
        "labels": ["#email"],
        "description": None,
        "latest_user_note": None,
    }
    result = await acts.classify_one(task)
    assert result["classification"] == "trash"
    assert "downgraded from 2_min" in result["reason"]


@pytest.mark.asyncio
async def test_classify_one_keeps_genuine_2min(db_pool) -> None:
    """A genuine #email 2-min action is left as 2_min (guard must not
    over-fire)."""
    llm = AsyncMock()
    llm.think = AsyncMock(
        return_value=json.dumps(
            {
                "classification": "2_min",
                "confidence": 0.9,
                "assignee": "@me",
                "contexts": ["@5min", "@email"],
                "reason": "quick reply",
            }
        )
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=llm)
    task = {
        "id": "T_REAL2M",
        "content": "Reply to the plumber to confirm Tuesday 9am",
        "source_tag": "#email",
        "labels": ["#email"],
        "description": None,
        "latest_user_note": None,
    }
    result = await acts.classify_one(task)
    assert result["classification"] == "2_min"


@pytest.mark.asyncio
async def test_classify_one_notification_guard_scoped_to_email(db_pool) -> None:
    """The 2_min notification downgrade is #email-scoped — a manually captured
    task whose title happens to match a marker is NOT downgraded."""
    llm = AsyncMock()
    llm.think = AsyncMock(
        return_value=json.dumps(
            {
                "classification": "2_min",
                "confidence": 0.9,
                "assignee": "@me",
                "contexts": ["@5min"],
                "reason": "quick",
            }
        )
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=llm)
    task = {
        "id": "T_MANUAL",
        "content": "Verify your new device works before the trip",
        "source_tag": "#manual",
        "labels": ["#manual"],
        "description": None,
        "latest_user_note": None,
    }
    result = await acts.classify_one(task)
    assert result["classification"] == "2_min"


def _seed_managed_projects(db_pool):
    """Return an async helper that seeds the canonical managed-project ids.

    Next/Someday are @next / @someday labels now, not managed projects
    (Todoist restructure, 2026-07) — only Inbox remains a managed project id.
    """

    async def _seed():
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES "
                "('todoist_managed_project_ids', $1) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                {
                    "inbox": "P_INBOX",
                },
            )

    return _seed


@pytest.mark.asyncio
async def test_apply_outcome_trash_completes_item(db_pool) -> None:
    await _seed_managed_projects(db_pool)()
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    task = {"id": "T_TRASH", "labels": ["#email"]}
    decision = {
        "classification": "trash",
        "confidence": 0.95,
        "assignee": "@me",
        "contexts": [],
        "reason": "spam",
        "llm_model": "qwen3:14b",
    }
    out = await acts.apply_outcome(task, decision)
    assert out["applied"] is True
    sent = connector.commands.await_args.args[0]
    types = [c["type"] for c in sent]
    assert "item_update" in types  # add #trash label
    assert "item_complete" in types
    assert "note_add" in types


@pytest.mark.asyncio
async def test_apply_outcome_reference_adds_label_no_move(db_pool) -> None:
    """Reference classification: add @reference to labels, do NOT item_move."""
    await _seed_managed_projects(db_pool)()
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    task = {"id": "T_REF", "labels": ["#research"]}
    decision = {
        "classification": "reference",
        "confidence": 1.0,
        "assignee": "@raphael",
        "contexts": ["@reading"],
        "reason": "informational",
        "llm_model": "rules",
    }
    out = await acts.apply_outcome(task, decision)
    assert out["applied"] is True
    sent = connector.commands.await_args.args[0]
    types = [c["type"] for c in sent]
    # No project move under the state-as-label model
    assert "item_move" not in types
    # State label landed on the item_update
    upd = next(c for c in sent if c["type"] == "item_update")
    assert "@reference" in upd["args"]["labels"]
    assert "@raphael" in upd["args"]["labels"]
    # Note still added
    note = next(c for c in sent if c["type"] == "note_add")
    assert "@raphael" in note["args"]["content"]
    assert "reference" in note["args"]["content"]


@pytest.mark.asyncio
async def test_apply_outcome_someday_adds_label_no_move(db_pool) -> None:
    """Someday classification: add @someday to labels, do NOT item_move
    (mirrors the reference branch under the state-as-label model)."""
    await _seed_managed_projects(db_pool)()
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    task = {"id": "T_SOM", "labels": ["#read"]}
    decision = {
        "classification": "someday",
        "confidence": 0.92,
        "assignee": "@me",
        "contexts": [],
        "reason": "future maybe",
        "llm_model": "rules",
    }
    out = await acts.apply_outcome(task, decision)
    assert out["applied"] is True
    sent = connector.commands.await_args.args[0]
    types = [c["type"] for c in sent]
    # No project move under the state-as-label model
    assert "item_move" not in types
    # State label landed on the item_update
    upd = next(c for c in sent if c["type"] == "item_update")
    assert "@someday" in upd["args"]["labels"]
    assert "@me" in upd["args"]["labels"]


async def _override_user_timezone(db_pool, tz: str) -> str:
    """Set settings.user_timezone to `tz` and return the previous value for restore.

    The project's asyncpg pool registers a JSONB codec (json.dumps/json.loads).
    Pass the bare Python string with NO ::jsonb cast and NO manual
    json.dumps — the codec handles encoding, and a cast would force
    text-mode interpretation that bypasses the codec (this is the
    Phase 1 lesson on asyncpg JSONB double-encoding).
    """
    async with db_pool.acquire() as conn:
        prev = await conn.fetchval("SELECT value FROM settings WHERE key='user_timezone'")
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('user_timezone', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            tz,
        )
    return prev if isinstance(prev, str) else "UTC"


async def _restore_user_timezone(db_pool, tz: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('user_timezone', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            tz,
        )


@pytest.mark.asyncio
async def test_apply_outcome_2min_in_window_returns_interaction_payload(db_pool) -> None:
    """During 8h-22h local, a 2_min classification returns an interaction_payload
    for ClarifyFlow to spawn — no Todoist commands sent yet."""
    await _seed_managed_projects(db_pool)()
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('gtd_2min_rule_enabled', 'true'::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
    prev_tz = await _override_user_timezone(db_pool, "UTC")
    try:
        connector = AsyncMock()
        connector.commands = AsyncMock(
            return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
        )
        acts = ClarifyActivities(
            db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock()
        )
        # Freeze "now" to a known in-window timestamp (12:00 UTC = 12:00 local in this test)
        fixed = dt.datetime(2026, 5, 19, 12, 0, 0, tzinfo=dt.UTC)
        task = {"id": "T_2MIN", "content": "Reply to vendor", "labels": ["#email"]}
        decision = {
            "classification": "2_min",
            "confidence": 0.9,
            "assignee": "@me",
            "contexts": ["@5min"],
            "reason": "quick reply",
            "llm_model": "qwen3:14b",
            "source_tag": "#email",
        }
        out = await acts.apply_outcome(task, decision, _now=fixed)
        assert out["applied"] is False
        assert out["interaction_spawned"] is True
        payload = out["interaction_payload"]
        assert payload["flavor"] == "2_min"
        assert "Reply to vendor" in payload["prompt"]
        assert set(payload["options"].keys()) == {"do_now", "defer_1d", "trash"}
        assert payload["decision"]["classification"] == "2_min"
        assert payload["pass_n"] == 1
        # No Todoist commands sent in this path (the resolution activity will)
        connector.commands.assert_not_awaited()
    finally:
        await _restore_user_timezone(db_pool, prev_tz)


@pytest.mark.asyncio
async def test_apply_outcome_2min_force_apply_does_now_action(db_pool) -> None:
    """force_apply=True on a 2_min classification skips the interaction and
    writes the @5min label update directly (used by apply_clarify_resolution
    for 'do_now')."""
    await _seed_managed_projects(db_pool)()
    prev_tz = await _override_user_timezone(db_pool, "UTC")
    try:
        connector = AsyncMock()
        connector.commands = AsyncMock(
            return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
        )
        acts = ClarifyActivities(
            db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock()
        )
        fixed = dt.datetime(2026, 5, 19, 12, 0, 0, tzinfo=dt.UTC)
        task = {"id": "T_2MIN_FORCE", "content": "x", "labels": ["#email"]}
        decision = {
            "classification": "2_min",
            "confidence": 0.9,
            "assignee": "@me",
            "contexts": ["@5min"],
            "reason": "quick reply",
            "llm_model": "user_resolution",
            "source_tag": "#email",
        }
        out = await acts.apply_outcome(task, decision, _now=fixed, force_apply=True)
        assert out["applied"] is True
        assert out["interaction_spawned"] is False
        sent = connector.commands.await_args.args[0]
        update = next(c for c in sent if c["type"] == "item_update")
        assert "@5min" in update["args"]["labels"]
    finally:
        await _restore_user_timezone(db_pool, prev_tz)


@pytest.mark.asyncio
async def test_apply_outcome_2min_out_of_window_demotes_to_next_action(db_pool) -> None:
    await _seed_managed_projects(db_pool)()
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('gtd_2min_rule_enabled', 'true'::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
    prev_tz = await _override_user_timezone(db_pool, "UTC")
    try:
        connector = AsyncMock()
        connector.commands = AsyncMock(
            return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
        )
        acts = ClarifyActivities(
            db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock()
        )
        # 03:00 UTC = 03:00 local = OUT of 8-22 window
        fixed = dt.datetime(2026, 5, 19, 3, 0, 0, tzinfo=dt.UTC)
        task = {"id": "T_2MIN_OUT", "labels": ["#email"]}
        decision = {
            "classification": "2_min",
            "confidence": 0.9,
            "assignee": "@me",
            "contexts": ["@5min"],
            "reason": "quick reply",
            "llm_model": "qwen3:14b",
        }
        out = await acts.apply_outcome(task, decision, _now=fixed)
        assert out["applied"] is True
        assert out["interaction_spawned"] is False
        # No interaction; just label + comment update applied
        sent = connector.commands.await_args.args[0]
        update = next(c for c in sent if c["type"] == "item_update")
        assert "@5min" in update["args"]["labels"]
    finally:
        await _restore_user_timezone(db_pool, prev_tz)


@pytest.mark.asyncio
async def test_apply_outcome_low_confidence_no_apply(db_pool) -> None:
    """confidence < escalation_threshold → applied=False, interaction spawned."""
    await _seed_managed_projects(db_pool)()
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    task = {"id": "T_LOW", "labels": ["#email"]}
    decision = {
        "classification": "someday",
        "confidence": 0.4,
        "assignee": "@me",
        "contexts": ["@reading"],
        "reason": "unsure",
        "llm_model": "claude-sonnet",
    }
    out = await acts.apply_outcome(task, decision)
    assert out["applied"] is False
    assert out["interaction_spawned"] is True
    # interaction_payload included (Phase 4)
    payload = out["interaction_payload"]
    assert payload["flavor"] == "low_conf"
    assert set(payload["options"].keys()) == {"confirm", "trash", "leave"}
    assert payload["decision"]["classification"] == "someday"
    # Only the NEEDS REVIEW note is sent; no item_update / item_move
    sent = connector.commands.await_args.args[0]
    types = [c["type"] for c in sent]
    assert types == ["note_add"]
    assert "NEEDS REVIEW" in sent[0]["args"]["content"]


@pytest.mark.asyncio
async def test_apply_outcome_low_conf_force_apply_bypasses_interaction(db_pool) -> None:
    """force_apply=True applies a low-confidence decision directly (used
    when the user picked 'confirm' on the review card)."""
    await _seed_managed_projects(db_pool)()
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    task = {"id": "T_LOW_FORCE", "content": "x", "labels": ["#email"]}
    decision = {
        "classification": "next_action",
        "confidence": 0.4,
        "assignee": "@sebas",
        "contexts": ["@email"],
        "reason": "user confirmed",
        "llm_model": "user_resolution",
        "source_tag": "#email",
    }
    out = await acts.apply_outcome(task, decision, force_apply=True)
    assert out["applied"] is True
    assert out["interaction_spawned"] is False
    sent = connector.commands.await_args.args[0]
    assert any(c["type"] == "item_update" for c in sent)
    assert any(c["type"] == "note_add" for c in sent)


@pytest.mark.asyncio
async def test_apply_outcome_leave_label(db_pool) -> None:
    """classification='leave' → @review label only (low-conf 'Leave for later')."""
    await _seed_managed_projects(db_pool)()
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    task = {"id": "T_LEAVE", "content": "x", "labels": ["#email"]}
    decision = {
        "classification": "leave",
        "confidence": 1.0,
        "assignee": "@me",
        "contexts": [],
        "reason": "user deferred",
        "llm_model": "user_resolution",
        "source_tag": "#email",
    }
    out = await acts.apply_outcome(task, decision, force_apply=True)
    assert out["applied"] is True
    sent = connector.commands.await_args.args[0]
    update = next(c for c in sent if c["type"] == "item_update")
    assert "@review" in update["args"]["labels"]
    # Importantly: no item_move (stays in Inbox)
    assert all(c["type"] != "item_move" for c in sent)


@pytest.mark.asyncio
async def test_apply_outcome_due_string_passes_through(db_pool) -> None:
    """next_action + decision._due_string adds due={'string': ...} (used by
    'defer_1d' resolution)."""
    await _seed_managed_projects(db_pool)()
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    task = {"id": "T_DUE", "content": "x", "labels": ["#email"]}
    decision = {
        "classification": "next_action",
        "confidence": 0.9,
        "assignee": "@sebas",
        "contexts": ["@5min"],
        "reason": "deferred",
        "llm_model": "user_resolution",
        "source_tag": "#email",
        "_due_string": "tomorrow",
    }
    out = await acts.apply_outcome(task, decision, force_apply=True)
    assert out["applied"] is True
    sent = connector.commands.await_args.args[0]
    update = next(c for c in sent if c["type"] == "item_update")
    assert update["args"].get("due") == {"string": "tomorrow"}


@pytest.mark.asyncio
async def test_apply_outcome_retryable_failure_enqueues_outbox(db_pool) -> None:
    """When Todoist commands() returns ok=False + retryable=True, every
    command in the batch lands in todoist_outbox so drain_outbox can
    replay them on the next tick. Without this, a transient Todoist 5xx
    leaves the task half-clarified — the audit log row records
    applied=False, last_clarified_at gets bumped, and the task is
    never retried automatically.
    """
    await _seed_managed_projects(db_pool)()
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM todoist_outbox WHERE temp_id LIKE 'apply-%'")
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": False, "error": "http_500", "retryable": True}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    task = {"id": "T_RETRY", "content": "spam", "labels": ["#manual"]}
    decision = {
        "classification": "trash",
        "confidence": 0.95,
        "assignee": "@me",
        "contexts": [],
        "reason": "spam",
        "llm_model": "qwen3:14b",
    }
    out = await acts.apply_outcome(task, decision)
    assert out["applied"] is False
    assert out["outbox_queued"] == 3  # item_update + item_complete + note_add
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT command->>'type' AS type FROM todoist_outbox "
            "WHERE command->>'args' LIKE '%T_RETRY%' AND status='pending' "
            "ORDER BY id"
        )
    types = [r["type"] for r in rows]
    assert "item_update" in types
    assert "item_complete" in types
    assert "note_add" in types


@pytest.mark.asyncio
async def test_apply_outcome_non_retryable_failure_does_not_enqueue(db_pool) -> None:
    """A non-retryable failure (4xx) should NOT queue to outbox — retrying
    a malformed command would just fail again. Just log applied=False.
    """
    await _seed_managed_projects(db_pool)()
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM todoist_outbox WHERE temp_id LIKE 'apply-%'")
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": False, "error": "http_400", "retryable": False}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    task = {"id": "T_BAD", "content": "x", "labels": ["#email"]}
    decision = {
        "classification": "next_action",
        "confidence": 0.9,
        "assignee": "@sebas",
        "contexts": ["@email"],
        "reason": "test",
        "llm_model": "qwen3:14b",
    }
    out = await acts.apply_outcome(task, decision)
    assert out["applied"] is False
    assert out["outbox_queued"] == 0


@pytest.mark.asyncio
async def test_apply_outcome_project_seed_degrades_to_label_only(db_pool) -> None:
    """project_seed is retired (multi-step uses subtasks). A stray
    project_seed classification degrades to a label-only update — no
    sub-project, no move, no define-outcome task."""
    await _seed_managed_projects(db_pool)()
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    task = {"id": "T_SEED", "content": "Move to new flat", "labels": ["#manual"]}
    decision = {
        "classification": "project_seed",
        "confidence": 0.92,
        "assignee": "@me",
        "contexts": ["@deep"],
        "reason": "multistep",
        "llm_model": "claude-sonnet",
    }
    out = await acts.apply_outcome(task, decision)
    assert out["applied"] is True
    sent = connector.commands.await_args.args[0]
    types = [c["type"] for c in sent]
    assert "project_add" not in types
    assert "item_move" not in types
    assert "item_add" not in types
    assert "item_update" in types


@pytest.mark.asyncio
async def test_log_classification_inserts_row_and_bumps_last_clarified_at(db_pool) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('P_INBOX','Inbox',true,'{}'::jsonb) ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, labels, source_tag, raw) "
            "VALUES ('T_LOG','P_INBOX','hello',ARRAY['#email'],'#email','{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING"
        )
        # Reset to clean state in case of prior test runs
        await conn.execute("DELETE FROM gtd_clarify_log WHERE todoist_task_id='T_LOG'")
        await conn.execute("UPDATE todoist_tasks SET last_clarified_at=NULL WHERE id='T_LOG'")
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock())
    decision = {
        "classification": "next_action",
        "confidence": 0.85,
        "assignee": "@sebas",
        "contexts": ["@email", "@5min"],
        "reason": "reply due",
        "llm_model": "qwen3:14b",
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "latency_ms": 800,
    }
    await acts.log_classification(
        task_id="T_LOG",
        decision=decision,
        applied=True,
        pass_n=1,
        user_hint=None,
    )
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT classification, confidence, assignee, contexts, reason, "
            "llm_model, prompt_tokens, completion_tokens, latency_ms, applied, pass "
            "FROM gtd_clarify_log WHERE todoist_task_id='T_LOG'"
        )
        last_clar = await conn.fetchval(
            "SELECT last_clarified_at FROM todoist_tasks WHERE id='T_LOG'"
        )
    assert row["classification"] == "next_action"
    assert row["confidence"] == pytest.approx(0.85)
    assert row["assignee"] == "@sebas"
    assert list(row["contexts"]) == ["@email", "@5min"]
    assert row["llm_model"] == "qwen3:14b"
    assert row["prompt_tokens"] == 120
    assert row["completion_tokens"] == 30
    assert row["latency_ms"] == 800
    assert row["applied"] is True
    assert row["pass"] == 1
    assert last_clar is not None


@pytest.mark.asyncio
async def test_log_classification_preserves_user_hint(db_pool) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('P_INBOX','Inbox',true,'{}'::jsonb) ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, labels, source_tag, raw) "
            "VALUES ('T_HINT','P_INBOX','hello',ARRAY['#email'],'#email','{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute("DELETE FROM gtd_clarify_log WHERE todoist_task_id='T_HINT'")
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock())
    await acts.log_classification(
        task_id="T_HINT",
        decision={
            "classification": "reference",
            "confidence": 0.92,
            "assignee": "@raphael",
            "contexts": ["@reading"],
            "reason": "user",
            "llm_model": "claude-sonnet",
        },
        applied=True,
        pass_n=2,
        user_hint="not action — save for later",
    )
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_hint, pass FROM gtd_clarify_log WHERE todoist_task_id='T_HINT'"
        )
    assert row["user_hint"] == "not action — save for later"
    assert row["pass"] == 2


# --- Phase 4: apply_clarify_resolution dispatch table ---


@pytest_asyncio.fixture(loop_scope="function")
async def _resolution_task(db_pool):
    """Seed a task that apply_clarify_resolution can re-read labels from."""
    await _seed_managed_projects(db_pool)()
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('P_INBOX','Inbox',true,'{}'::jsonb) ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute("DELETE FROM gtd_clarify_log WHERE todoist_task_id='T_RES'")
        await conn.execute("DELETE FROM todoist_tasks WHERE id='T_RES'")
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, labels, source_tag, raw) "
            "VALUES ('T_RES','P_INBOX','Reply to vendor',"
            "ARRAY['#email'],'#email','{}'::jsonb)"
        )


def _decision_2min() -> dict:
    return {
        "classification": "2_min",
        "confidence": 0.9,
        "assignee": "@sebas",
        "contexts": ["@5min", "@email"],
        "reason": "fast reply",
        "llm_model": "qwen3:14b",
        "source_tag": "#email",
    }


def _decision_low_conf() -> dict:
    return {
        "classification": "someday",
        "confidence": 0.42,
        "assignee": "@raphael",
        "contexts": ["@reading"],
        "reason": "informational",
        "llm_model": "claude-sonnet",
        "source_tag": "#email",
    }


@pytest.mark.asyncio
async def test_apply_clarify_resolution_2min_do_now(db_pool, _resolution_task) -> None:
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    metadata = {
        "source": "gtd_clarify",
        "flavor": "2_min",
        "task_id": "T_RES",
        "decision": _decision_2min(),
        "pass_n": 1,
    }
    out = await acts.apply_clarify_resolution(
        "11111111-1111-1111-1111-111111111111",
        {"value": "do_now"},
        metadata,
    )
    assert out["applied"] is True
    assert out["choice"] == "do_now"
    sent = connector.commands.await_args.args[0]
    update = next(c for c in sent if c["type"] == "item_update")
    assert "@5min" in update["args"]["labels"]
    # Audit row written with pass=2 + user_resolution model
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT pass, llm_model, applied, user_hint FROM gtd_clarify_log "
            "WHERE todoist_task_id='T_RES' ORDER BY created_at DESC LIMIT 1"
        )
    assert row["pass"] == 2
    assert row["llm_model"] == "user_resolution"
    assert row["applied"] is True
    assert row["user_hint"] == "chat:do_now"


@pytest.mark.asyncio
async def test_apply_clarify_resolution_2min_defer(db_pool, _resolution_task) -> None:
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    metadata = {
        "source": "gtd_clarify",
        "flavor": "2_min",
        "task_id": "T_RES",
        "decision": _decision_2min(),
        "pass_n": 1,
    }
    out = await acts.apply_clarify_resolution(
        "22222222-2222-2222-2222-222222222222",
        {"value": "defer_1d"},
        metadata,
    )
    assert out["applied"] is True
    sent = connector.commands.await_args.args[0]
    update = next(c for c in sent if c["type"] == "item_update")
    assert update["args"].get("due") == {"string": "tomorrow"}


@pytest.mark.asyncio
async def test_apply_clarify_resolution_2min_trash(db_pool, _resolution_task) -> None:
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    metadata = {
        "source": "gtd_clarify",
        "flavor": "2_min",
        "task_id": "T_RES",
        "decision": _decision_2min(),
        "pass_n": 1,
    }
    out = await acts.apply_clarify_resolution(
        "33333333-3333-3333-3333-333333333333",
        {"value": "trash"},
        metadata,
    )
    assert out["applied"] is True
    sent = connector.commands.await_args.args[0]
    assert any(c["type"] == "item_complete" for c in sent)


@pytest.mark.asyncio
async def test_apply_clarify_resolution_low_conf_confirm(db_pool, _resolution_task) -> None:
    """confirm → apply the LLM's original classification verbatim
    (force_apply bypasses the confidence floor)."""
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    decision = _decision_low_conf()  # classification=someday, conf=0.42
    metadata = {
        "source": "gtd_clarify",
        "flavor": "low_conf",
        "task_id": "T_RES",
        "decision": decision,
        "pass_n": 1,
    }
    out = await acts.apply_clarify_resolution(
        "44444444-4444-4444-4444-444444444444",
        {"value": "confirm"},
        metadata,
    )
    assert out["applied"] is True
    sent = connector.commands.await_args.args[0]
    # someday → @someday label, no item_move
    types = [c["type"] for c in sent]
    assert "item_move" not in types
    upd = next(c for c in sent if c["type"] == "item_update")
    assert "@someday" in upd["args"]["labels"]


@pytest.mark.asyncio
async def test_apply_clarify_resolution_low_conf_trash(db_pool, _resolution_task) -> None:
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    metadata = {
        "source": "gtd_clarify",
        "flavor": "low_conf",
        "task_id": "T_RES",
        "decision": _decision_low_conf(),
        "pass_n": 1,
    }
    out = await acts.apply_clarify_resolution(
        "55555555-5555-5555-5555-555555555555",
        {"value": "trash"},
        metadata,
    )
    assert out["applied"] is True
    sent = connector.commands.await_args.args[0]
    assert any(c["type"] == "item_complete" for c in sent)


@pytest.mark.asyncio
async def test_apply_clarify_resolution_low_conf_leave(db_pool, _resolution_task) -> None:
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    metadata = {
        "source": "gtd_clarify",
        "flavor": "low_conf",
        "task_id": "T_RES",
        "decision": _decision_low_conf(),
        "pass_n": 1,
    }
    out = await acts.apply_clarify_resolution(
        "66666666-6666-6666-6666-666666666666",
        {"value": "leave"},
        metadata,
    )
    assert out["applied"] is True
    sent = connector.commands.await_args.args[0]
    update = next(c for c in sent if c["type"] == "item_update")
    assert "@review" in update["args"]["labels"]
    # No item_move (stays in Inbox)
    assert all(c["type"] != "item_move" for c in sent)


@pytest.mark.asyncio
async def test_apply_clarify_resolution_unknown_choice_no_apply(db_pool, _resolution_task) -> None:
    connector = AsyncMock()
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    metadata = {
        "source": "gtd_clarify",
        "flavor": "2_min",
        "task_id": "T_RES",
        "decision": _decision_2min(),
        "pass_n": 1,
    }
    out = await acts.apply_clarify_resolution(
        "77777777-7777-7777-7777-777777777777",
        {"value": "nonsense"},
        metadata,
    )
    assert out["applied"] is False
    assert "unknown_2min_choice" in out["reason"]
    connector.commands.assert_not_called()


# --- Inbox gate: pandora_gate card + @me hands-off (2026-07) ---


def _decision_gate() -> dict:
    return {
        "classification": "pandora_gate",
        "confidence": 1.0,
        "assignee": "@pandora",
        "contexts": ["@deep", "@code"],
        "reason": "APP-<n>: jira ticket — ask before investigating",
        "llm_model": "rules",
        "source_tag": None,
    }


@pytest.mark.asyncio
async def test_apply_outcome_pandora_gate_spawns_card(db_pool) -> None:
    """pandora_gate spawns a two-option choice card and sends NO Todoist
    commands — nothing touches the ticket until the user picks."""
    connector = AsyncMock()
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    task = {
        "id": "T_GATE",
        "content": "APP-11175: Ownership importer not writing internal id",
        "labels": [],
        "source_tag": None,
    }
    out = await acts.apply_outcome(task, _decision_gate())
    assert out["applied"] is False
    assert out["interaction_spawned"] is True
    payload = out["interaction_payload"]
    assert payload["flavor"] == "pandora_gate"
    # No spawn_kind → ClarifyFlow routes to InteractionFlow, not the alert flow.
    assert "spawn_kind" not in payload
    assert set(payload["options"]) == {"investigate", "mine"}
    assert out["commands_sent"] == 0
    connector.commands.assert_not_called()


@pytest.mark.asyncio
async def test_apply_outcome_mine_adds_me_label(db_pool) -> None:
    """The 'mine' classification stamps @me (hands-off) and does not complete."""
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    task = {"id": "T_MINE", "content": "APP-1: x", "labels": ["@area/acme"], "source_tag": None}
    decision = {
        "classification": "mine",
        "confidence": 1.0,
        "assignee": "@me",
        "contexts": [],
        "reason": "resolved via chat",
        "llm_model": "user_resolution",
    }
    out = await acts.apply_outcome(task, decision, force_apply=True)
    assert out["applied"] is True
    sent = connector.commands.await_args.args[0]
    upd = next(c for c in sent if c["type"] == "item_update")
    assert "@me" in upd["args"]["labels"]
    assert not any(c["type"] == "item_complete" for c in sent)


@pytest.mark.asyncio
async def test_apply_clarify_resolution_pandora_gate_investigate(db_pool, _resolution_task) -> None:
    """Gate 'investigate' stamps @pandora + @area/acme AND clears the watermark
    so the next tick re-surfaces the task for the real AlertInvestigationFlow
    spawn (child-workflow spawns must happen in the flow, not this activity)."""
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    # Pre-bump the watermark so a passing NULL assertion proves the clear ran.
    # Give the task an APP-<n>: title so it matches the seeded content route —
    # the route's area_label (@area/acme) is what gets stamped on investigate.
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE todoist_tasks SET content='APP-500: vendor issue', "
            "last_clarified_at = now() WHERE id='T_RES'"
        )
    metadata = {
        "source": "gtd_clarify",
        "flavor": "pandora_gate",
        "task_id": "T_RES",
        "decision": _decision_gate(),
        "pass_n": 1,
    }
    out = await acts.apply_clarify_resolution(
        "88888888-8888-8888-8888-888888888888",
        {"value": "investigate"},
        metadata,
    )
    assert out["applied"] is True
    assert out["choice"] == "investigate"
    sent = connector.commands.await_args.args[0]
    upd = next(c for c in sent if c["type"] == "item_update")
    assert "@pandora" in upd["args"]["labels"]
    assert "@area/acme" in upd["args"]["labels"]
    async with db_pool.acquire() as conn:
        wm = await conn.fetchval("SELECT last_clarified_at FROM todoist_tasks WHERE id='T_RES'")
    assert wm is None


@pytest.mark.asyncio
async def test_apply_clarify_resolution_pandora_gate_mine(db_pool, _resolution_task) -> None:
    """Gate 'I've got it' stamps @me (hands off); the watermark stays bumped so
    the task is doubly excluded from clarify."""
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    metadata = {
        "source": "gtd_clarify",
        "flavor": "pandora_gate",
        "task_id": "T_RES",
        "decision": _decision_gate(),
        "pass_n": 1,
    }
    out = await acts.apply_clarify_resolution(
        "99999999-9999-9999-9999-999999999999",
        {"value": "mine"},
        metadata,
    )
    assert out["applied"] is True
    assert out["choice"] == "mine"
    sent = connector.commands.await_args.args[0]
    upd = next(c for c in sent if c["type"] == "item_update")
    assert "@me" in upd["args"]["labels"]
    async with db_pool.acquire() as conn:
        wm = await conn.fetchval("SELECT last_clarified_at FROM todoist_tasks WHERE id='T_RES'")
    assert wm is not None


@pytest.mark.asyncio
async def test_apply_clarify_resolution_pandora_gate_unknown_choice(db_pool, _resolution_task) -> None:
    connector = AsyncMock()
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    metadata = {
        "source": "gtd_clarify",
        "flavor": "pandora_gate",
        "task_id": "T_RES",
        "decision": _decision_gate(),
        "pass_n": 1,
    }
    out = await acts.apply_clarify_resolution(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        {"value": "nonsense"},
        metadata,
    )
    assert out["applied"] is False
    assert "unknown_gate_choice" in out["reason"]
    connector.commands.assert_not_called()


@pytest_asyncio.fixture(loop_scope="function")
async def _inbox_with_me_tasks(db_pool):
    """Seed two Inbox tasks: one claimed with @me (hands off), one claimed with
    @me but ALSO addressed to @raphael (agent routing must still win)."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"inbox": "P_INBOX", "projects": "P_PRJ", "single_actions": "P_SA"},
        )
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('P_INBOX','Inbox',true,'{}'::jsonb) ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute("DELETE FROM todoist_tasks WHERE id IN ('T_ME','T_ME_RAPHAEL')")
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, source_tag, is_completed, raw) "
            "VALUES ('T_ME','P_INBOX','I will do this myself',"
            "ARRAY['@me'],'#email',false,'{}'::jsonb)"
        )
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, source_tag, is_completed, raw) "
            "VALUES ('T_ME_RAPHAEL','P_INBOX','ask raphael',"
            "ARRAY['@me','@raphael'],'#email',false,'{}'::jsonb)"
        )


@pytest.mark.asyncio
async def test_find_unclassified_excludes_me_handsoff(db_pool, _inbox_with_me_tasks) -> None:
    """A task claimed with @me is hands-off — clarify skips it. But @me
    co-occurring with an agent alias still passes so the comment-channel
    reply path keeps firing. (inbox gate)"""
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock())
    rows = await acts.find_unclassified_items(max_items=50)
    ids = {r["id"] for r in rows}
    assert "T_ME" not in ids
    assert "T_ME_RAPHAEL" in ids


# --- Phase 5: ingest_reference_to_ks ---


def test_extract_first_url_finds_https_in_prose() -> None:
    assert (
        ClarifyActivities._extract_first_url("See https://example.com/article for details.")
        == "https://example.com/article"
    )
    assert ClarifyActivities._extract_first_url("no url here just words") is None
    # Trailing punctuation stripped
    assert (
        ClarifyActivities._extract_first_url("https://example.com/x, and more")
        == "https://example.com/x"
    )
    # Picks first if multiple
    assert (
        ClarifyActivities._extract_first_url("see https://a.com and https://b.com")
        == "https://a.com"
    )


@pytest.mark.asyncio
async def test_ingest_reference_to_ks_with_url() -> None:
    """When task content contains a URL, KS gets the URL (no raw_text);
    KS will fetch + extract via its own scraper."""
    kc = AsyncMock()
    kc.ingest_content = AsyncMock(
        return_value={"status": "accepted", "job_id": "JOB-1", "content_id": "C-1"}
    )
    acts = ClarifyActivities(
        db_pool=None,
        todoist_connector=AsyncMock(),
        llm_client=AsyncMock(),
        knowledge_connector=kc,
    )
    out = await acts.ingest_reference_to_ks(
        task_id="T_REF1",
        task_content="Apple WWDC 2026 keynote at https://apple.com/wwdc",
        task_description="",
        source_tag="#research",
        latest_user_note=None,
    )
    assert out["status"] == "ok"
    assert out["url"] == "https://apple.com/wwdc"
    assert out["content_id"] == "C-1"
    kc.ingest_content.assert_awaited_once()
    kwargs = kc.ingest_content.await_args.kwargs
    assert kwargs["url"] == "https://apple.com/wwdc"
    assert kwargs["source_type"] == "reference"
    assert "gtd:reference" in kwargs["tags"]
    assert "#research" in kwargs["tags"]
    assert kwargs["metadata"]["todoist_task_id"] == "T_REF1"
    # No raw_text when URL is present — KS does extraction
    assert "raw_text" not in kwargs


@pytest.mark.asyncio
async def test_ingest_reference_to_ks_without_url_sends_raw_text() -> None:
    """No URL → synthetic aegis://reference/<id> URL + task text as raw_text."""
    kc = AsyncMock()
    kc.ingest_content = AsyncMock(return_value={"status": "ok", "content_id": "C-2"})
    acts = ClarifyActivities(
        db_pool=None,
        todoist_connector=AsyncMock(),
        llm_client=AsyncMock(),
        knowledge_connector=kc,
    )
    out = await acts.ingest_reference_to_ks(
        task_id="T_REF2",
        task_content="GTD: capture is when, clarify is what",
        task_description="From a podcast episode I listened to.",
        source_tag="#manual",
        latest_user_note=None,
    )
    assert out["status"] == "ok"
    assert out["url"] is None
    kwargs = kc.ingest_content.await_args.kwargs
    assert kwargs["url"] == "aegis://reference/T_REF2"
    assert "raw_text" in kwargs
    assert "GTD: capture" in kwargs["raw_text"]
    assert "podcast" in kwargs["raw_text"]


@pytest.mark.asyncio
async def test_ingest_reference_to_ks_gmail_url_falls_through_to_raw_text() -> None:
    """Gmail deeplinks aren't scrapable from KS's network — the route must
    drop the URL and send the description (which now carries the body
    excerpt stashed by gmail_ingest) as raw_text."""
    kc = AsyncMock()
    kc.ingest_content = AsyncMock(return_value={"status": "ok", "content_id": "C-GM"})
    acts = ClarifyActivities(
        db_pool=None,
        todoist_connector=AsyncMock(),
        llm_client=AsyncMock(),
        knowledge_connector=kc,
    )
    out = await acts.ingest_reference_to_ks(
        task_id="T_GM",
        task_content="Welcome to Reddit for Business",
        task_description=(
            "From: reddit\n\n[Open in Gmail](https://mail.google.com/mail/u/0/#inbox/19e4)\n\n"
            "---\n\nFull email body text with details about features and pricing."
        ),
        source_tag="#email",
        latest_user_note=None,
    )
    assert out["status"] == "ok"
    kwargs = kc.ingest_content.await_args.kwargs
    # URL falls back to synthetic — the gmail deeplink would have made
    # KS try to scrape a Google login page.
    assert kwargs["url"] == "aegis://reference/T_GM"
    assert "Full email body text" in kwargs["raw_text"]


@pytest.mark.asyncio
async def test_ingest_reference_to_ks_hn_url_fetches_body(monkeypatch) -> None:
    """HN item URLs aren't usefully scrapable as HTML — the route fetches
    the Firebase API body + top comments and ships them as raw_text."""
    kc = AsyncMock()
    kc.ingest_content = AsyncMock(return_value={"status": "ok", "content_id": "C-HN"})
    acts = ClarifyActivities(
        db_pool=None,
        todoist_connector=AsyncMock(),
        llm_client=AsyncMock(),
        knowledge_connector=kc,
    )

    async def fake_hn(item_id: str) -> str | None:
        assert item_id == "47911524"
        return "AI agent deleted prod DB.\n\nComment 1.\n\nComment 2."

    monkeypatch.setattr(ClarifyActivities, "_fetch_hn_body", staticmethod(fake_hn))
    out = await acts.ingest_reference_to_ks(
        task_id="T_HN",
        task_content="An AI agent deleted our production database",
        task_description="https://news.ycombinator.com/item?id=47911524",
        source_tag="#research",
        latest_user_note=None,
    )
    assert out["status"] == "ok"
    kwargs = kc.ingest_content.await_args.kwargs
    assert kwargs["url"] == "aegis://reference/T_HN"
    assert "AI agent deleted prod DB" in kwargs["raw_text"]
    assert "Comment 1" in kwargs["raw_text"]


def test_hn_item_id_extracts_from_url() -> None:
    assert (
        ClarifyActivities._hn_item_id("https://news.ycombinator.com/item?id=47911524") == "47911524"
    )
    assert ClarifyActivities._hn_item_id("https://news.ycombinator.com/news") is None
    assert ClarifyActivities._hn_item_id("") is None
    assert ClarifyActivities._hn_item_id(None) is None  # type: ignore[arg-type]


def test_url_is_unscrapable_matches_gmail_and_hn() -> None:
    assert ClarifyActivities._url_is_unscrapable("https://mail.google.com/mail/u/0/#inbox/abc")
    assert ClarifyActivities._url_is_unscrapable("https://news.ycombinator.com/item?id=1")
    assert not ClarifyActivities._url_is_unscrapable("https://apple.com/wwdc")
    assert not ClarifyActivities._url_is_unscrapable("")


@pytest.mark.asyncio
async def test_ingest_reference_to_ks_transient_raises() -> None:
    """KS unreachable / 5xx → raises so Temporal retries the activity."""
    kc = AsyncMock()
    kc.ingest_content = AsyncMock(side_effect=RuntimeError("KS down"))
    acts = ClarifyActivities(
        db_pool=None,
        todoist_connector=AsyncMock(),
        llm_client=AsyncMock(),
        knowledge_connector=kc,
    )
    with pytest.raises(RuntimeError, match="KS down"):
        await acts.ingest_reference_to_ks(
            task_id="T_REF3",
            task_content="x",
            task_description="",
            source_tag="#research",
            latest_user_note=None,
        )


@pytest.mark.asyncio
async def test_ingest_reference_to_ks_permanent_4xx_returns_verdict() -> None:
    """KS responding 4xx → verdict {status: permanent_error}, no raise."""
    import httpx

    kc = AsyncMock()
    request = httpx.Request("POST", "http://ks/api/content")
    response = httpx.Response(404, request=request, json={"detail": "not found"})
    kc.ingest_content = AsyncMock(
        side_effect=httpx.HTTPStatusError("404", request=request, response=response)
    )
    acts = ClarifyActivities(
        db_pool=None,
        todoist_connector=AsyncMock(),
        llm_client=AsyncMock(),
        knowledge_connector=kc,
    )
    out = await acts.ingest_reference_to_ks(
        task_id="T_REF_404",
        task_content="x",
        task_description="",
        source_tag="#research",
        latest_user_note=None,
    )
    assert out["status"] == "permanent_error"
    assert "http_404" in out["reason"]


@pytest.mark.asyncio
async def test_ingest_reference_to_ks_5xx_raises_for_retry() -> None:
    """KS responding 5xx → classified as transient → raises for Temporal."""
    import httpx

    kc = AsyncMock()
    request = httpx.Request("POST", "http://ks/api/content")
    response = httpx.Response(503, request=request, json={"detail": "down"})
    kc.ingest_content = AsyncMock(
        side_effect=httpx.HTTPStatusError("503", request=request, response=response)
    )
    acts = ClarifyActivities(
        db_pool=None,
        todoist_connector=AsyncMock(),
        llm_client=AsyncMock(),
        knowledge_connector=kc,
    )
    with pytest.raises(httpx.HTTPStatusError):
        await acts.ingest_reference_to_ks(
            task_id="T_REF_503",
            task_content="x",
            task_description="",
            source_tag="#research",
            latest_user_note=None,
        )


@pytest.mark.asyncio
async def test_ingest_reference_to_ks_skips_when_no_connector() -> None:
    acts = ClarifyActivities(
        db_pool=None,
        todoist_connector=AsyncMock(),
        llm_client=AsyncMock(),
        knowledge_connector=None,
    )
    out = await acts.ingest_reference_to_ks(
        task_id="T_REF4",
        task_content="x",
        task_description="",
        source_tag="#research",
        latest_user_note=None,
    )
    assert out["status"] == "skipped"
    assert out["reason"] == "no_knowledge_connector"


@pytest.mark.asyncio
async def test_apply_clarify_resolution_low_conf_confirm_ingests_reference(
    db_pool, _resolution_task
) -> None:
    """When user resolves a low-conf interaction by 'confirm' AND the LLM's
    original classification was 'reference', apply_clarify_resolution
    triggers ingest_reference_to_ks inline."""
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    kc = AsyncMock()
    kc.ingest_content = AsyncMock(return_value={"status": "ok", "content_id": "C-RES-1"})
    acts = ClarifyActivities(
        db_pool=db_pool,
        todoist_connector=connector,
        llm_client=AsyncMock(),
        knowledge_connector=kc,
    )
    decision = {
        "classification": "reference",
        "confidence": 0.45,
        "assignee": "@raphael",
        "contexts": ["@reading"],
        "reason": "Possibly a research link",
        "llm_model": "claude-sonnet",
        "source_tag": "#email",
    }
    metadata = {
        "source": "gtd_clarify",
        "flavor": "low_conf",
        "task_id": "T_RES",
        "decision": decision,
        "pass_n": 1,
    }
    await acts.apply_clarify_resolution(
        "abc-confirm",
        {"value": "confirm"},
        metadata,
    )
    # KS ingest fired because resolved classification == 'reference'
    kc.ingest_content.assert_awaited()


# ── Pandora-investigation branch (2026-05-20) ─────────────────────────


@pytest.mark.asyncio
async def test_classify_one_app_prefix_returns_pandora_gate(db_pool) -> None:
    """A FRESH task (no @pandora) whose content starts with APP-<n>: skips the
    LLM and routes to the pandora_gate choice card — NOT a silent
    investigation. (inbox gate — user asks before an agent runs.)"""
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock())
    task = {
        "id": "T_APP",
        "content": "APP-12345: Portfolio valuation incorrect for ZAR positions",
        "labels": [],
        "source_tag": "#manual",
    }
    decision = await acts.classify_one(task)
    assert decision["classification"] == "pandora_gate"
    assert decision["confidence"] == 1.0
    assert decision["assignee"] == "@pandora"
    assert decision["llm_model"] == "rules"


@pytest.mark.asyncio
async def test_classify_one_app_prefix_requires_colon(db_pool) -> None:
    """Bare mentions of APP-1234 (no colon) fall through to the LLM."""
    llm = AsyncMock()
    llm.think = AsyncMock(
        return_value={
            "response": json.dumps(
                {
                    "classification": "reference",
                    "confidence": 0.9,
                    "assignee": "@me",
                    "contexts": [],
                    "reason": "test",
                }
            ),
            "model": "qwen3:14b",
            "prompt_tokens": 1,
            "completion_tokens": 1,
        }
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=llm)
    task = {
        "id": "T_NOPP",
        "content": "APP-12345 was discussed yesterday",
        "labels": [],
        "source_tag": "#manual",
    }
    decision = await acts.classify_one(task)
    # LLM-routed — not a pandora branch (neither gate nor investigation)
    assert decision["classification"] not in {"pandora_investigation", "pandora_gate"}


@pytest.mark.asyncio
async def test_classify_one_gate_false_route_applies_labels_directly(db_pool) -> None:
    """A content route with gate:false routes by content to `route_apply` — the
    route's assignee + contexts (+ area_label) are applied directly, no choice
    card and no agent run. Proves routing is config-driven, not hardcoded to
    APP-/@pandora: here a `contains "[bug]"` route hands the task to @raphael."""
    from aegis.services.content_routes import save_content_routes
    from aegis_worker.activities import clarify as _cl

    await save_content_routes(
        db_pool,
        [
            {
                "key": "bug",
                "match": "contains",
                "value": "[bug]",
                "gate": False,
                "assignee": "@raphael",
                "contexts": ["@reading"],
                "area_label": "@area/oss",
            }
        ],
    )
    _cl._routes_cache.update(routes=None, ts=0.0)
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    task = {"id": "T_BUG", "content": "urgent [bug] in parser", "labels": [], "source_tag": None}

    decision = await acts.classify_one(task)
    assert decision["classification"] == "route_apply"
    assert decision["assignee"] == "@raphael"

    out = await acts.apply_outcome(task, decision)
    assert out["applied"] is True
    assert out["interaction_spawned"] is False
    sent = connector.commands.await_args.args[0]
    upd = next(c for c in sent if c["type"] == "item_update")
    assert {"@raphael", "@reading", "@area/oss"} <= set(upd["args"]["labels"])
    assert not any(c["type"] == "item_complete" for c in sent)


@pytest.mark.asyncio
async def test_classify_one_pandora_label_short_circuits(db_pool) -> None:
    """Tasks already carrying @pandora bypass classification entirely."""
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock())
    task = {
        "id": "T_OWNED",
        "content": "Anything at all",
        "labels": ["#alert", "@pandora"],
        "source_tag": "#alert",
    }
    decision = await acts.classify_one(task)
    assert decision["classification"] == "pandora_owned"
    assert decision["confidence"] == 1.0
    assert decision["llm_model"] == "rules"


@pytest.mark.asyncio
async def test_classify_one_pandora_app_with_completed_investigation_stays_owned(
    db_pool,
) -> None:
    """@pandora APP-<n>: task with a SUCCESSFUL prior investigation stays
    pandora_owned (no retry — the run did its job)."""
    task_id = "T_PANDORA_DONE"
    async with db_pool.acquire() as conn:
        # Seed a completed run for this task
        await conn.execute(
            """
            INSERT INTO workflow_runs
              (run_id, workflow_id, workflow_type, agent_id, status,
               started_at, completed_at, duration_ms)
            VALUES ($1, $2, 'AlertInvestigationFlow', 'pandoras-actor', 'completed',
                    NOW() - INTERVAL '2 hours', NOW() - INTERVAL '1 hour', 3600000)
            ON CONFLICT (run_id) DO NOTHING
            """,
            f"run-{task_id}",
            f"pandora-jira-{task_id}-scheduled-x",
        )
    try:
        acts = ClarifyActivities(
            db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock()
        )
        task = {
            "id": task_id,
            "content": "APP-12345: real ticket",
            "labels": ["@pandora", "@area/acme"],
            "source_tag": None,
        }
        decision = await acts.classify_one(task)
        assert decision["classification"] == "pandora_owned"
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM workflow_runs WHERE run_id = $1", f"run-{task_id}")


@pytest.mark.asyncio
async def test_classify_one_pandora_app_without_investigation_retries(db_pool) -> None:
    """@pandora APP-<n>: task with NO successful prior investigation
    re-routes to pandora_investigation (retry — prior attempt crashed)."""
    task_id = "T_PANDORA_RETRY"
    # No prior run for this task — verify the helper sees none
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM workflow_runs WHERE workflow_id LIKE $1",
            f"pandora-jira-{task_id}-%",
        )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock())
    task = {
        "id": task_id,
        "content": "APP-99999: never investigated",
        "labels": ["@pandora"],
        "source_tag": None,
    }
    decision = await acts.classify_one(task)
    assert decision["classification"] == "pandora_investigation"
    assert "retry" in decision["reason"].lower() or "no successful" in decision["reason"].lower()


@pytest.mark.asyncio
async def test_classify_one_pandora_app_with_only_failed_runs_still_retries(
    db_pool,
) -> None:
    """A previously-FAILED AlertInvestigationFlow does NOT count as
    completed — the retry branch still fires."""
    task_id = "T_PANDORA_FAILED_ONLY"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM workflow_runs WHERE workflow_id LIKE $1",
            f"pandora-jira-{task_id}-%",
        )
        await conn.execute(
            """
            INSERT INTO workflow_runs
              (run_id, workflow_id, workflow_type, agent_id, status,
               started_at, completed_at, duration_ms, error)
            VALUES ($1, $2, 'AlertInvestigationFlow', 'pandoras-actor', 'failed',
                    NOW() - INTERVAL '6 hours', NOW() - INTERVAL '5 hours',
                    3600000, 'ActivityError: timed out')
            ON CONFLICT (run_id) DO NOTHING
            """,
            f"run-{task_id}",
            f"pandora-jira-{task_id}-scheduled-x",
        )
    try:
        acts = ClarifyActivities(
            db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock()
        )
        task = {
            "id": task_id,
            "content": "APP-77777: failed earlier",
            "labels": ["@pandora"],
        }
        decision = await acts.classify_one(task)
        assert decision["classification"] == "pandora_investigation"
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM workflow_runs WHERE run_id = $1", f"run-{task_id}")


@pytest.mark.asyncio
async def test_apply_outcome_pandora_owned_is_noop(db_pool) -> None:
    """pandora_owned applies no Todoist commands but logs as applied."""
    await _seed_managed_projects(db_pool)()
    connector = AsyncMock()
    connector.commands = AsyncMock(return_value={"ok": True, "data": {}})
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    task = {"id": "T_OWNED", "labels": ["@pandora"]}
    decision = {
        "classification": "pandora_owned",
        "confidence": 1.0,
        "assignee": "@pandora",
        "contexts": [],
        "reason": "owned",
        "llm_model": "rules",
    }
    out = await acts.apply_outcome(task, decision)
    assert out["applied"] is True
    assert out["commands_sent"] == 0
    connector.commands.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_outcome_pandora_investigation_labels_and_signal(db_pool) -> None:
    """pandora_investigation stamps the area+pandora labels and returns a
    spawn payload carrying the alert dict for AlertInvestigationFlow."""
    await _seed_managed_projects(db_pool)()
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    task = {
        "id": "T_APP",
        "content": "APP-12345: Portfolio valuation incorrect for ZAR positions",
        "description": "Spotted on 2026-05-20",
        "labels": ["#manual"],
    }
    decision = {
        "classification": "pandora_investigation",
        "confidence": 1.0,
        "assignee": "@pandora",
        "contexts": ["@deep", "@code"],
        "reason": "detected APP-<n>: jira-key prefix",
        "llm_model": "rules",
    }
    out = await acts.apply_outcome(task, decision)
    assert out["applied"] is True
    assert out["interaction_spawned"] is True
    payload = out["interaction_payload"]
    assert payload["spawn_kind"] == "pandora_investigation"
    alert = payload["alert"]
    assert alert["todoist_task_id"] == "T_APP"
    assert alert["resource_tag_filter"] == ["acme"]
    assert alert["requires_approval"] is False
    assert alert["source"] == "todoist-jira"
    assert alert["fingerprint"] == "route-T_APP"
    # item_update was sent with both labels
    sent = connector.commands.await_args.args[0]
    upd = next(c for c in sent if c["type"] == "item_update")
    assert "@area/acme" in upd["args"]["labels"]
    assert "@pandora" in upd["args"]["labels"]


@pytest_asyncio.fixture(loop_scope="function")
async def _inbox_with_app_jira(db_pool):
    """Seed an inbox task that mimics a Jira-synced APP- ticket: no source_tag."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"inbox": "P_INBOX", "projects": "P_PRJ", "single_actions": "P_SA"},
        )
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('P_INBOX','Inbox',true,'{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute("DELETE FROM todoist_tasks WHERE id='T_APP_JIRA'")
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, labels, source_tag, is_completed, raw) "
            "VALUES ('T_APP_JIRA','P_INBOX',"
            "'APP-9955: Spy: remove dead code and upgrade python',"
            # Fresh Jira sync carries no @me (labels come verbatim from
            # Todoist); @me would trip the inbox-gate hands-off exclusion.
            "ARRAY['@area/acme'],NULL,false,'{}'::jsonb)"
        )


@pytest.mark.asyncio
async def test_find_unclassified_includes_app_jira_without_source_tag(
    db_pool, _inbox_with_app_jira
) -> None:
    """APP-<n>: tasks synced from Jira have no source_tag but must still
    enter clarify so the pandora_investigation branch can fire."""
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock(), llm_client=AsyncMock())
    rows = await acts.find_unclassified_items(max_items=10)
    found = [r for r in rows if r["id"] == "T_APP_JIRA"]
    assert len(found) == 1
    assert found[0]["source_tag"] is None
    assert found[0]["content"].startswith("APP-9955:")


# ── Audit fixes: per-command status + watermark invariant + followup lane ──


@pytest.mark.asyncio
async def test_apply_outcome_per_command_rejected_returns_applied_false(db_pool) -> None:
    """When envelope is ok but per-command sync_status reports failure
    (ITEM_NOT_FOUND, INVALID_ARGUMENT, etc.), applied must be False —
    even though envelope-ok would have masked it pre-fix."""
    await _seed_managed_projects(db_pool)()
    connector = AsyncMock()

    async def _commands(cmds):
        # Reject every command with ITEM_NOT_FOUND (the stale-projection
        # case that prompted the original silent-failure fix).
        return {
            "ok": True,
            "data": {
                "sync_status": {
                    c["uuid"]: {
                        "error": "Item not found",
                        "error_code": 22,
                        "error_tag": "ITEM_NOT_FOUND",
                        "http_code": 404,
                    }
                    for c in cmds
                },
                "temp_id_mapping": {},
            },
        }

    connector.commands = AsyncMock(side_effect=_commands)
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    task = {
        "id": "T_DEAD",
        "content": "APP-9001: stale ticket",
        "labels": [],
    }
    decision = {
        "classification": "pandora_investigation",
        "confidence": 1.0,
        "assignee": "@pandora",
        "contexts": ["@deep", "@code"],
        "reason": "x",
        "llm_model": "rules",
    }
    out = await acts.apply_outcome(task, decision)
    assert out["applied"] is False, (
        "envelope-ok + per-cmd ITEM_NOT_FOUND must surface as applied=False"
    )
    # Non-retryable → outbox NOT queued (would just poison-loop otherwise)
    assert out["outbox_queued"] == 0


@pytest.mark.asyncio
async def test_apply_outcome_per_command_5xx_queues_outbox(db_pool) -> None:
    """Transient per-cmd 5xx (rejected_retryable=True) → outbox compensation."""
    await _seed_managed_projects(db_pool)()
    connector = AsyncMock()

    async def _commands(cmds):
        return {
            "ok": True,
            "data": {
                "sync_status": {
                    c["uuid"]: {
                        "error": "server error",
                        "error_tag": "SERVICE_UNAVAILABLE",
                        "http_code": 503,
                    }
                    for c in cmds
                },
                "temp_id_mapping": {},
            },
        }

    connector.commands = AsyncMock(side_effect=_commands)
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    task = {"id": "T_503", "content": "APP-9002: x", "labels": []}
    decision = {
        "classification": "pandora_investigation",
        "confidence": 1.0,
        "assignee": "@pandora",
        "contexts": ["@deep", "@code"],
        "reason": "x",
        "llm_model": "rules",
    }
    out = await acts.apply_outcome(task, decision)
    assert out["applied"] is False
    assert out["outbox_queued"] > 0, "503 per-cmd should be queued for retry"


@pytest.mark.asyncio
async def test_log_classification_skips_watermark_when_bump_false(db_pool) -> None:
    """bump_watermark=False writes the audit row but leaves last_clarified_at NULL."""
    await _seed_managed_projects(db_pool)()
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, labels, is_completed, raw) "
            "VALUES ('T_WM', 'P_INBOX', 'x', ARRAY[]::text[], false, '{}'::jsonb) "
            "ON CONFLICT (id) DO UPDATE SET last_clarified_at = NULL"
        )
    acts = ClarifyActivities(db_pool=db_pool)
    decision = {
        "classification": "next_action",
        "confidence": 0.9,
        "assignee": "@me",
        "contexts": [],
        "reason": "x",
        "llm_model": "rules",
    }
    await acts.log_classification(
        "T_WM", decision, applied=False, pass_n=1, user_hint=None, bump_watermark=False
    )
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT last_clarified_at FROM todoist_tasks WHERE id='T_WM'")
    assert row["last_clarified_at"] is None, "watermark must stay NULL when bump=False"


@pytest.mark.asyncio
async def test_classify_one_detects_pandora_followup_on_user_comment(db_pool) -> None:
    """When an @pandora-labelled APP-* task has a fresh user comment,
    classify_one routes to pandora_followup (not pandora_owned), which
    triggers a fresh delta-investigation in apply_outcome."""
    acts = ClarifyActivities(db_pool=db_pool, llm_client=AsyncMock())
    task = {
        "id": "T_FOLLOWUP",
        "content": "APP-10741: > rule bug on screener",
        "labels": ["@pandora", "@area/acme"],
        "latest_user_note": "Reproduced on screener-p-server staging too",
        "last_note_at": dt.datetime(2026, 5, 21, 10, 30, tzinfo=dt.UTC),
    }
    decision = await acts.classify_one(task)
    assert decision["classification"] == "pandora_followup"
    assert decision["confidence"] == 1.0


@pytest.mark.asyncio
async def test_classify_one_pandora_owned_when_no_user_comment(db_pool) -> None:
    """No user comment + a SUCCESSFUL prior investigation → pandora_owned
    (the historical short-circuit; the retry branch added 2026-05-21
    only fires when no completed run exists for this task)."""
    task_id = "T_OWNED2"
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO workflow_runs
              (run_id, workflow_id, workflow_type, agent_id, status,
               started_at, completed_at, duration_ms)
            VALUES ($1, $2, 'AlertInvestigationFlow', 'pandoras-actor', 'completed',
                    NOW() - INTERVAL '2 hours', NOW() - INTERVAL '1 hour', 3600000)
            ON CONFLICT (run_id) DO NOTHING
            """,
            f"run-{task_id}",
            f"pandora-jira-{task_id}-scheduled-x",
        )
    try:
        acts = ClarifyActivities(db_pool=db_pool, llm_client=AsyncMock())
        task = {
            "id": task_id,
            "content": "APP-10741: > rule bug on screener",
            "labels": ["@pandora", "@area/acme"],
            "latest_user_note": None,
        }
        decision = await acts.classify_one(task)
        assert decision["classification"] == "pandora_owned"
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM workflow_runs WHERE run_id = $1", f"run-{task_id}")


@pytest.mark.asyncio
async def test_apply_outcome_pandora_followup_builds_followup_alert(db_pool) -> None:
    """pandora_followup builds an alert dict with the user's comment
    appended to the description and a unique per-comment fingerprint
    (so the alert-flow 24h dedup doesn't block re-investigations)."""
    await _seed_managed_projects(db_pool)()
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=connector, llm_client=AsyncMock())
    task = {
        "id": "T_FU",
        "content": "APP-10741: > rule bug",
        "description": "Original report",
        "labels": ["@pandora", "@area/acme"],
        "latest_user_note": "Also affects production — escalate",
        "last_note_at": dt.datetime(2026, 5, 21, 11, 0, tzinfo=dt.UTC),
    }
    decision = {
        "classification": "pandora_followup",
        "confidence": 1.0,
        "assignee": "@pandora",
        "contexts": ["@deep", "@code"],
        "reason": "x",
        "llm_model": "rules",
    }
    out = await acts.apply_outcome(task, decision)
    assert out["applied"] is True
    assert out["interaction_spawned"] is True
    alert = out["interaction_payload"]["alert"]
    assert alert["todoist_task_id"] == "T_FU"
    # Fingerprint includes a token derived from last_note_at (per-comment)
    assert alert["fingerprint"].startswith("route-T_FU-followup-")
    assert alert["fingerprint"] != "route-T_FU"
    # User comment carried into description
    assert "Also affects production" in alert["description"]
    assert "User followup comment" in alert["description"]


# --- references-as-knowledge: completion & demotion paths ---


@pytest.mark.asyncio
async def test_complete_reference_task_issues_complete_and_skips_notify_for_automated() -> None:
    """Automated source (#research) → completes task, no per-message chat notification."""
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    delivery = AsyncMock()
    delivery.channel = "slack"
    acts = ClarifyActivities(
        db_pool=None,
        todoist_connector=connector,
        llm_client=AsyncMock(),
        delivery_connector=delivery,
    )
    out = await acts.complete_reference_task(
        task_id="T_REF_OK",
        title="Some long article",
        source_tag="#research",
        content_id="ks-content-1",
        url="https://example.com/x",
    )
    assert out["completed"] is True
    assert out["notify_sent"] is False
    assert out["automated_source"] is True
    sent = connector.commands.await_args.args[0]
    types = [c["type"] for c in sent]
    assert "note_add" in types
    assert "item_complete" in types
    delivery.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_complete_reference_task_sends_notify_for_user_initiated() -> None:
    """User-initiated source (#chat) → per-message confirmation from raphael."""
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    delivery = AsyncMock()
    delivery.channel = "slack"
    delivery.send_message = AsyncMock(return_value={"ok": True})
    acts = ClarifyActivities(
        db_pool=None,
        todoist_connector=connector,
        llm_client=AsyncMock(),
        delivery_connector=delivery,
    )
    out = await acts.complete_reference_task(
        task_id="T_REF_USER",
        title="Article shared in chat",
        source_tag="#chat",
        content_id="ks-content-2",
        url=None,
    )
    assert out["completed"] is True
    assert out["notify_sent"] is True
    delivery.send_message.assert_awaited_once()
    kwargs = delivery.send_message.await_args.kwargs
    assert kwargs["agent_id"] == "raphael"
    assert "Filed" in kwargs["message"]
    assert "Article shared in chat" in kwargs["message"]


@pytest.mark.asyncio
async def test_complete_reference_task_returns_retryable_on_5xx() -> None:
    """Envelope says retryable=True → caller can compensate."""
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": False, "retryable": True, "error": "http_503", "data": None}
    )
    acts = ClarifyActivities(
        db_pool=None,
        todoist_connector=connector,
        llm_client=AsyncMock(),
        delivery_connector=AsyncMock(),
    )
    out = await acts.complete_reference_task(
        task_id="T_REF_5XX",
        title="x",
        source_tag="#research",
        content_id="ks-id",
        url=None,
    )
    assert out["completed"] is False
    assert out["retryable"] is True


@pytest.mark.asyncio
async def test_reclassify_reference_to_reading_swaps_labels_and_notifies() -> None:
    """Permanent ingest failure → strip @reference, add @to-read, raphael notifies in chat."""
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    delivery = AsyncMock()
    delivery.channel = "slack"
    delivery.send_message = AsyncMock(return_value={"ok": True})
    acts = ClarifyActivities(
        db_pool=None,
        todoist_connector=connector,
        llm_client=AsyncMock(),
        delivery_connector=delivery,
    )
    out = await acts.reclassify_reference_to_reading(
        task_id="T_REF_BAD",
        title="Article that 404'd",
        source_tag="#research",
        existing_labels=["@raphael", "@reference", "@reading"],
        reason="http_404: not found",
    )
    assert out["reclassified"] is True
    assert "@to-read" in out["labels"]
    assert "@reference" not in out["labels"]
    # Always notifies on demote — the user has a new reading task to act on.
    assert out["notify_sent"] is True
    delivery.send_message.assert_awaited_once()
    msg = delivery.send_message.await_args.kwargs["message"]
    assert "Couldn't file" in msg
    assert "http_404" in msg
    # Note added with the reason
    sent_commands = connector.commands.await_args.args[0]
    note_cmds = [c for c in sent_commands if c["type"] == "note_add"]
    assert note_cmds
    assert "http_404" in note_cmds[0]["args"]["content"]


@pytest.mark.asyncio
async def test_reclassify_handles_missing_delivery_connector() -> None:
    """No delivery wired (unit test mode) → reclassify still succeeds; notify is a no-op."""
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(
        db_pool=None,
        todoist_connector=connector,
        llm_client=AsyncMock(),
        delivery_connector=None,
    )
    out = await acts.reclassify_reference_to_reading(
        task_id="T_REF_NO_DELIVERY",
        title="x",
        source_tag="#research",
        existing_labels=["@reference"],
        reason="http_404",
    )
    assert out["reclassified"] is True
    assert out["notify_sent"] is False


# --- Bundle B sub-fix #3: safe_send_message for notify paths ---


@pytest.mark.asyncio
async def test_notify_reference_filed_uses_safe_send_message_swallows_failure() -> None:
    """Pin (2026-05-28): _notify_reference_filed routes through
    safe_send_message so an exception in send_message does NOT bubble
    out of the activity. The reclassify path still completes. We assert
    by making send_message raise — pre-fix this would have raised out
    of the activity (the wrapping try/except returned False); post-fix
    the helper swallows it and we return True (dispatch happened)."""
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    delivery = AsyncMock()
    delivery.channel = "slack"
    delivery.send_message = AsyncMock(side_effect=RuntimeError("simulated network glitch"))
    acts = ClarifyActivities(
        db_pool=None,
        todoist_connector=connector,
        llm_client=AsyncMock(),
        delivery_connector=delivery,
    )
    # Should NOT raise.
    out = await acts.complete_reference_task(
        task_id="T_REF_SAFE",
        title="Article",
        source_tag="#chat",  # user-initiated → triggers notify
        content_id="ks-id-safe",
        url=None,
    )
    assert out["completed"] is True
    # safe_send_message is non-raising; we dispatched the send (return True).
    assert out["notify_sent"] is True
    delivery.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_reference_demoted_uses_safe_send_message_swallows_failure() -> None:
    """Companion for the demote path."""
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    delivery = AsyncMock()
    delivery.channel = "slack"
    delivery.send_message = AsyncMock(side_effect=RuntimeError("boom"))
    acts = ClarifyActivities(
        db_pool=None,
        todoist_connector=connector,
        llm_client=AsyncMock(),
        delivery_connector=delivery,
    )
    out = await acts.reclassify_reference_to_reading(
        task_id="T_REF_DEMOTE_SAFE",
        title="x",
        source_tag="#research",
        existing_labels=["@reference"],
        reason="http_404",
    )
    assert out["reclassified"] is True
    assert out["notify_sent"] is True
    delivery.send_message.assert_awaited_once()


# --- Bundle B sub-fix #4: KS-ingest retry loop in apply_clarify_resolution ---


@pytest.mark.asyncio
async def test_apply_clarify_resolution_retries_ks_ingest_on_transient(
    db_pool, _resolution_task
) -> None:
    """Pin (2026-05-28): apply_clarify_resolution wraps
    ingest_reference_to_ks in a 3-attempt retry loop. A transient
    (raising) failure on attempts 1 and 2 followed by success on
    attempt 3 should still complete the reference cleanly — pre-fix
    the first raise demoted the task to @to-read.
    """
    from unittest.mock import patch

    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(
        db_pool=db_pool,
        todoist_connector=connector,
        llm_client=AsyncMock(),
        delivery_connector=AsyncMock(),
    )

    call_count = {"n": 0}

    async def flaky_ingest(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise RuntimeError(f"transient_{call_count['n']}")
        return {"status": "ok", "url": None, "content_id": "ks-id-flaky", "job_id": "J"}

    # Stub the inline `await asyncio.sleep` so the test doesn't actually
    # wait 1+2 = 3 real seconds.
    async def _no_sleep(_secs):
        return None

    metadata = {
        "source": "gtd_clarify",
        "flavor": "low_conf",
        "task_id": "T_RES",
        "decision": {
            "classification": "reference",
            "confidence": 0.5,
            "assignee": "@raphael",
            "contexts": ["@reading"],
            "reason": "",
            "llm_model": "test",
            "source_tag": "#chat",
        },
        "pass_n": 1,
    }
    with (
        patch.object(acts, "ingest_reference_to_ks", side_effect=flaky_ingest),
        patch("aegis_worker.activities.clarify.asyncio.sleep", new=_no_sleep),
    ):
        await acts.apply_clarify_resolution(
            "I_FLAKY",
            {"value": "confirm"},
            metadata,
        )
    assert call_count["n"] == 3, f"expected 3 attempts, got {call_count['n']}"
    # No demotion occurred — connector.commands was called for the original
    # reference apply + the completion (item_complete). Should NOT contain
    # a reclassify-to-reading note_add ("@to-read").
    all_command_calls = connector.commands.await_args_list
    flat_cmds = [c for call in all_command_calls for c in call.args[0]]
    label_updates = [c for c in flat_cmds if c.get("type") == "item_update"]
    for upd in label_updates:
        labels = upd.get("args", {}).get("labels") or []
        assert "@to-read" not in labels, f"unexpected demote in {upd!r}"
    # Task ended up completed via complete_reference_task path
    completes = [c for c in flat_cmds if c.get("type") == "item_complete"]
    assert completes, "expected item_complete after retry succeeded"


@pytest.mark.asyncio
async def test_apply_clarify_resolution_demotes_after_retries_exhausted(
    db_pool, _resolution_task
) -> None:
    """When ALL 3 inline attempts raise transient errors, the verdict
    becomes inline_retries_exhausted (permanent_error) and the task
    is demoted to @to-read."""
    from unittest.mock import patch

    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    acts = ClarifyActivities(
        db_pool=db_pool,
        todoist_connector=connector,
        llm_client=AsyncMock(),
        delivery_connector=AsyncMock(),
    )
    call_count = {"n": 0}

    async def always_raises(*args, **kwargs):
        call_count["n"] += 1
        raise RuntimeError("upstream_5xx")

    async def _no_sleep(_secs):
        return None

    metadata = {
        "source": "gtd_clarify",
        "flavor": "low_conf",
        "task_id": "T_RES",
        "decision": {
            "classification": "reference",
            "confidence": 0.5,
            "assignee": "@raphael",
            "contexts": ["@reading"],
            "reason": "",
            "llm_model": "test",
            "source_tag": "#chat",
        },
        "pass_n": 1,
    }
    with (
        patch.object(acts, "ingest_reference_to_ks", side_effect=always_raises),
        patch("aegis_worker.activities.clarify.asyncio.sleep", new=_no_sleep),
    ):
        await acts.apply_clarify_resolution(
            "I_EXH",
            {"value": "confirm"},
            metadata,
        )

    assert call_count["n"] == 3
    # @to-read landed because the verdict became permanent_error after
    # inline retries exhausted.
    all_command_calls = connector.commands.await_args_list
    flat_cmds = [c for call in all_command_calls for c in call.args[0]]
    demote_updates = [
        c
        for c in flat_cmds
        if c.get("type") == "item_update" and "@to-read" in (c.get("args", {}).get("labels") or [])
    ]
    assert demote_updates, "expected @to-read demote after inline retries exhausted"


# --- Bundle B sub-fix #5: _fetch_recent_task_notes filtering ---


@pytest.mark.asyncio
async def test_fetch_recent_task_notes_excludes_clarify_and_workflow_notes(db_pool) -> None:
    """SQL filter (post-2026-05-28): exclude `[ClarifyFlow @ ...]` and
    `Workflow run: ...` notes from the transcript so the agent's prompt
    doesn't drown in machine noise, while KEEPING `[Agent reply @ ...]`
    notes (the agent NEEDS to see its past replies)."""
    import datetime as _dt

    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('PRJ_FN','p',true,'{}'::jsonb) ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute("DELETE FROM todoist_notes WHERE item_id = 'TASK_FN'")
        await conn.execute("DELETE FROM todoist_tasks WHERE id = 'TASK_FN'")
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, raw) "
            "VALUES ('TASK_FN','PRJ_FN','t',ARRAY['@pandora'],'{}'::jsonb)"
        )
        # 4 notes: user, clarify, agent reply, workflow-run
        await conn.execute(
            "DELETE FROM todoist_notes WHERE id IN ('N_FN_USER','N_FN_CLARIFY','N_FN_AGENT','N_FN_WFR')"
        )
        await conn.executemany(
            "INSERT INTO todoist_notes (id, item_id, content, posted_at, raw, updated_at) "
            "VALUES ($1, $2, $3, $4, '{}'::jsonb, now())",
            [
                (
                    "N_FN_USER",
                    "TASK_FN",
                    "user comment",
                    _dt.datetime(2026, 5, 27, 9, 0, tzinfo=_dt.UTC),
                ),
                (
                    "N_FN_CLARIFY",
                    "TASK_FN",
                    "[ClarifyFlow @ 2026-05-27 10:00 UTC · pass 1 · NEEDS REVIEW]\n details",
                    _dt.datetime(2026, 5, 27, 10, 0, tzinfo=_dt.UTC),
                ),
                (
                    "N_FN_AGENT",
                    "TASK_FN",
                    "[Agent reply @ 11:00 UTC agent=pandoras-actor]\nlooked at the code...",
                    _dt.datetime(2026, 5, 27, 11, 0, tzinfo=_dt.UTC),
                ),
                (
                    "N_FN_WFR",
                    "TASK_FN",
                    "Workflow run: AlertInvestigationFlow-xyz completed",
                    _dt.datetime(2026, 5, 27, 12, 0, tzinfo=_dt.UTC),
                ),
            ],
        )

    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=AsyncMock())
    notes = await acts._fetch_recent_task_notes("TASK_FN", limit=15)
    contents = [n["content"] for n in notes]
    assert any("user comment" in c for c in contents), f"user note missing: {contents}"
    assert any("Agent reply" in c for c in contents), f"agent reply missing: {contents}"
    assert not any("ClarifyFlow @" in c for c in contents), f"clarify leaked: {contents}"
    assert not any("Workflow run:" in c for c in contents), f"workflow run leaked: {contents}"


def test_build_agent_synthetic_input_warns_against_echoing_markers() -> None:
    """The transcript section must instruct the model NOT to reproduce the
    bracketed turn markers (Bug A: agents echoed `[Agent reply @ ...]` etc.
    into their own reply body)."""
    acts = ClarifyActivities(db_pool=None, todoist_connector=AsyncMock())
    recent_notes = [
        {
            "posted_at": dt.datetime(2026, 5, 27, 13, 27, tzinfo=dt.UTC),
            "content": "[Agent reply @ 13:27 UTC agent=pandoras-actor]\nI've kicked off the run.",
        },
    ]
    out = acts._build_agent_synthetic_input(
        {"id": "T1", "content": "fix it", "description": "", "latest_user_note": "status?"},
        "pandoras-actor",
        recent_notes=recent_notes,
    )
    assert "do NOT reproduce or echo them" in out
    assert "[Agent reply @ ...]" in out
    assert "Write only your" in out
