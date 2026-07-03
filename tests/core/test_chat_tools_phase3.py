"""Phase 3 chat-tool tests — schema + per-personality gating + executor smoke."""

from __future__ import annotations

import pytest
from aegis.services.chat import (
    AGENT_TOOL_SETS,  # noqa: F401 — used by Tasks 15/16 gating tests
    CHAT_TOOLS,
    TOOL_EXECUTORS,
)


def _names_in_chat_tools() -> set[str]:
    return {t["function"]["name"] for t in CHAT_TOOLS}


PHASE3_TOOLS = {
    "capture_to_inbox",
    "list_next_actions",
    "list_projects",
    "complete_task",
    "defer_task",
    "mark_waiting",
    "handoff_task",
    "find_reference",
}


@pytest.mark.parametrize("name", sorted([
    "capture_to_inbox", "list_next_actions", "list_projects",
    "complete_task", "defer_task",
    "mark_waiting", "handoff_task", "find_reference",
]))
def test_schema_registered(name: str) -> None:
    assert name in _names_in_chat_tools(), f"{name} missing from CHAT_TOOLS"


@pytest.mark.parametrize("name", sorted([
    "capture_to_inbox", "list_next_actions", "list_projects",
    "complete_task", "defer_task",
    "mark_waiting", "handoff_task", "find_reference",
]))
def test_executor_registered(name: str) -> None:
    assert name in TOOL_EXECUTORS, f"{name} missing from TOOL_EXECUTORS"


def test_create_project_tool_removed() -> None:
    """create_project retired in the projects→labels migration."""
    assert "create_project" not in TOOL_EXECUTORS
    assert "create_project" not in _names_in_chat_tools()


@pytest.mark.asyncio
async def test_exec_capture_to_inbox_calls_capture_helper(monkeypatch) -> None:
    """capture_to_inbox should delegate to the _capture_to_inbox_impl helper."""
    from aegis.services.chat import ToolContext, _exec_capture_to_inbox

    captured = {}

    async def fake_capture(pool, source_tag, external_id, title, description):
        captured.update({
            "source_tag": source_tag, "external_id": external_id,
            "title": title, "description": description,
        })
        return "TASK-REF-1"

    monkeypatch.setattr(
        "aegis.services.chat._capture_to_inbox_impl", fake_capture, raising=False
    )
    ctx = ToolContext(agent_id="sebas")
    result = await _exec_capture_to_inbox(
        pool=None,
        args={"text": "ping the team", "source": "chat"},
        ctx=ctx,
    )
    assert "TASK-REF-1" in result
    assert captured["source_tag"] == "#chat"
    assert captured["title"] == "ping the team"


@pytest.mark.asyncio
async def test_exec_list_next_actions_reads_projection(db_pool) -> None:
    from aegis.services.chat import ToolContext, _exec_list_next_actions

    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"inbox": "P_INBOX", "projects": "P_PRJ"},
        )
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('P_PRJ','Projects',true,'{}'::jsonb), "
            "      ('P_INBOX','Inbox',true,'{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING"
        )
        # Reset any prior state for these IDs
        await conn.execute("DELETE FROM todoist_tasks WHERE id IN ('T_NA','T_DONE')")
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, labels, assignee_label, is_completed, raw) "
            "VALUES "
            "('T_NA','P_PRJ','call vendor',ARRAY['@sebas','@phone'],'@sebas',false,'{}'::jsonb), "
            "('T_DONE','P_PRJ','old done',ARRAY['@sebas'],'@sebas',true,'{}'::jsonb)"
        )
    ctx = ToolContext(agent_id="sebas")
    out = await _exec_list_next_actions(
        pool=db_pool,
        args={"assignee": "@sebas", "limit": 50},
        ctx=ctx,
    )
    assert "T_NA" in out or "call vendor" in out
    assert "T_DONE" not in out and "old done" not in out


@pytest.mark.asyncio
async def test_exec_list_projects_returns_project_labels(db_pool) -> None:
    """list_projects now enumerates leaf work-stream projects (real Todoist
    projects nested under an AREA project via parent_id) with open-task
    counts — the retired project/* label convention is gone (Todoist
    restructure, 2026-07). AREA projects themselves (parent_id IS NULL)
    are not work-streams and must not appear.

    Uses unique project ids so the assertion is independent of other tests'
    nested-project data left in the shared DB."""
    from aegis.services.chat import ToolContext, _exec_list_projects

    area_id = "P_UITEST_AREA"
    ws_open_id = "P_UITEST_WSA"
    ws_empty_id = "P_UITEST_WSB"
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM todoist_tasks WHERE id IN ('T_UA1','T_UA2')")
        await conn.execute(
            "DELETE FROM todoist_projects WHERE id IN ($1,$2,$3)",
            ws_open_id, ws_empty_id, area_id,
        )
        # Parent AREA project (parent_id IS NULL) — must be excluded from
        # list_projects' output; only leaf work-streams are listed.
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, parent_id, is_managed, is_archived, raw) "
            "VALUES ($1, 'UITest Area', NULL, false, false, '{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING",
            area_id,
        )
        # Two leaf work-stream projects nested under the area.
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, parent_id, is_managed, is_archived, raw) "
            "VALUES ($1, 'UITest Alpha', $3, false, false, '{}'::jsonb), "
            "       ($2, 'UITest Beta', $3, false, false, '{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING",
            ws_open_id, ws_empty_id, area_id,
        )
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, is_completed, raw) "
            "VALUES "
            "('T_UA1',$1,'open',ARRAY['@me'],false,'{}'::jsonb), "
            "('T_UA2',$1,'done',ARRAY['@me'],true,'{}'::jsonb)",
            ws_open_id,
        )
    try:
        ctx = ToolContext(agent_id="sebas")
        out = await _exec_list_projects(pool=db_pool, args={}, ctx=ctx)
        # one OPEN task on alpha (the completed one is excluded); beta has none
        assert f"- [{ws_open_id}] UITest Alpha (1 open)" in out
        assert f"- [{ws_empty_id}] UITest Beta (0 open)" in out
        # the AREA project itself must not be listed as a work-stream
        assert area_id not in out
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM todoist_tasks WHERE id IN ('T_UA1','T_UA2')")
            await conn.execute(
                "DELETE FROM todoist_projects WHERE id IN ($1,$2,$3)",
                ws_open_id, ws_empty_id, area_id,
            )


# --- Phase 5: find_reference now queries KS with source_type filter ---


@pytest.mark.asyncio
async def test_exec_find_reference_queries_ks_with_source_type_filter(db_pool):
    """find_reference passes source_type='reference' to KS.search so the
    semantic search only hits the gtd-reference corpus."""
    from unittest.mock import AsyncMock

    from aegis.services.chat import ToolContext, _exec_find_reference

    captured: dict = {}

    async def fake_search(query, limit=10, source_type=None, tags=None, content_id=None):
        captured.update(
            {"query": query, "limit": limit, "source_type": source_type, "tags": tags}
        )
        return [
            {
                "content_id": "C-100",
                "title": "Apple WWDC 2026 keynote notes",
                "score": 0.91,
            }
        ]

    kc = AsyncMock()
    kc.search = AsyncMock(side_effect=fake_search)
    ctx = ToolContext(agent_id="sebas", knowledge_connector=kc)
    out = await _exec_find_reference(
        pool=None, args={"query": "WWDC keynote", "limit": 5}, ctx=ctx
    )
    assert captured["source_type"] == "reference"
    assert captured["query"] == "WWDC keynote"
    assert captured["limit"] == 5
    assert "Apple WWDC 2026 keynote notes" in out
    assert "score=0.91" in out


@pytest.mark.asyncio
async def test_exec_find_reference_falls_back_to_no_matches(db_pool):
    """When neither projection nor KS returns anything, return a friendly
    'no matches' message rather than empty string."""
    from unittest.mock import AsyncMock

    from aegis.services.chat import ToolContext, _exec_find_reference

    kc = AsyncMock()
    kc.search = AsyncMock(return_value=[])
    # Clear reference project setting so projection query returns nothing
    async with db_pool.acquire() as conn:
        # We don't delete other tests' settings; just point reference at
        # a project id with no matches.
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"reference": "P_NOMATCH"},
        )
    ctx = ToolContext(agent_id="sebas", knowledge_connector=kc)
    out = await _exec_find_reference(
        pool=db_pool, args={"query": "no-such-thing-xyz"}, ctx=ctx
    )
    assert out == "No reference matches."


# --- Per-personality gating ---

ALLOW_MATRIX = {
    "sebas":          {"capture_to_inbox", "list_next_actions", "list_projects",
                       "complete_task", "defer_task",
                       "mark_waiting", "handoff_task", "find_reference"},
    "raphael":        {"capture_to_inbox", "list_next_actions", "list_projects",
                       "complete_task", "handoff_task", "find_reference"},
    "maou":           {"capture_to_inbox", "list_next_actions", "list_projects",
                       "complete_task", "defer_task",
                       "mark_waiting", "handoff_task"},
    "pandoras-actor": {"capture_to_inbox", "list_next_actions", "list_projects",
                       "complete_task", "defer_task",
                       "handoff_task"},
}


@pytest.mark.parametrize("agent,expected", sorted(ALLOW_MATRIX.items()))
def test_per_personality_gating(agent: str, expected: set[str]) -> None:
    actual = AGENT_TOOL_SETS[agent]
    missing = expected - actual
    assert not missing, f"{agent} missing tools: {sorted(missing)}"
