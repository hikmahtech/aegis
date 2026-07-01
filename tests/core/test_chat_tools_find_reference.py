"""_exec_find_reference: query @reference label, not project_id."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from aegis.services.chat import ToolContext, _exec_find_reference


@pytest.mark.asyncio
async def test_find_reference_by_label_not_project(db_pool) -> None:
    async with db_pool.acquire() as conn:
        # Ensure parent projects exist (FK constraint).
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('P_BCP', 'BCP', false, '{}'::jsonb), "
            "('P_LEGACY_REF', 'Legacy Reference', false, '{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING"
        )
        # A task with @reference label but NOT in any "reference" project.
        await conn.execute(
            "INSERT INTO todoist_tasks (id, content, labels, project_id, "
            "is_completed) VALUES ('T_BCP_REF', 'BCP API spec link', $1, 'P_BCP', false) "
            "ON CONFLICT (id) DO UPDATE SET labels = EXCLUDED.labels",
            ["@reference", "@area/acme"],
        )
        # A task in a former-reference project ID but missing @reference label
        # — must NOT be returned under the new model.
        await conn.execute(
            "INSERT INTO todoist_tasks (id, content, labels, project_id, "
            "is_completed) VALUES ('T_OLD_REF', 'Outdated reference', $1, "
            "'P_LEGACY_REF', false) "
            "ON CONFLICT (id) DO UPDATE SET labels = EXCLUDED.labels",
            ["@me"],
        )

    with patch(
        "aegis.services.chat._exec_search_knowledge",
        return_value="",
    ):
        out = await _exec_find_reference(
            db_pool, {"query": "BCP", "limit": 10}, ToolContext(agent_id="sebas")
        )
    assert "T_BCP_REF" in out
    assert "T_OLD_REF" not in out
