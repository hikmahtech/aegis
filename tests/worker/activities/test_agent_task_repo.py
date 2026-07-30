"""resolve_task_repo — Todoist project name is the strongest repo signal."""

from __future__ import annotations

import pytest_asyncio
from aegis_worker.activities.agent_task import AgentTaskActivities


@pytest_asyncio.fixture(loop_scope="function")
async def _seed(db_pool):
    await db_pool.execute("DELETE FROM todoist_projects WHERE id LIKE 'pr-%'")
    await db_pool.execute("DELETE FROM resources WHERE slug LIKE 'test-repo-%'")
    await db_pool.execute(
        "INSERT INTO todoist_projects (id, name, is_managed, is_archived, order_idx) "
        "VALUES ('pr-bcp','BCP',false,false,1), ('pr-unknown','Nowhere',false,false,2)"
    )
    # `path` must be present AND nested, so a wrong JSONB key is catchable.
    await db_pool.execute(
        "INSERT INTO resources (slug, kind, title, metadata) VALUES "
        "('test-repo-bcp','repository','Stockopedia/bcp',"
        " '{\"github_repo\": \"Stockopedia/bcp\", \"path\": \"stockopedia/bcp\"}'::jsonb)"
    )
    yield
    await db_pool.execute("DELETE FROM todoist_projects WHERE id LIKE 'pr-%'")
    await db_pool.execute("DELETE FROM resources WHERE slug LIKE 'test-repo-%'")


async def test_project_name_resolves_to_repo(db_pool, _seed):
    act = AgentTaskActivities(db_pool=db_pool)
    result = await act.resolve_task_repo(
        {"id": "x", "content": "Fix the exporter", "project_id": "pr-bcp"}
    )
    assert result["github_repo"] == "Stockopedia/bcp"
    assert result["source"] == "project_map"
    # Load-bearing: proves the JSONB key is right. Without this the wrong key
    # ships green, flattening every nested checkout.
    assert result["repo_path"] == "stockopedia/bcp"


async def test_unmapped_project_returns_no_repo_never_a_guess(db_pool, _seed):
    act = AgentTaskActivities(db_pool=db_pool)
    result = await act.resolve_task_repo(
        {"id": "x", "content": "Fix something", "project_id": "pr-unknown"}
    )
    assert result["github_repo"] == ""
    assert result["source"] == "none"


async def test_missing_project_id_returns_no_repo(db_pool, _seed):
    act = AgentTaskActivities(db_pool=db_pool)
    result = await act.resolve_task_repo({"id": "x", "content": "Fix", "project_id": None})
    assert result["github_repo"] == ""
