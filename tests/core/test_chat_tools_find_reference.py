"""_exec_find_reference: query @reference label, not project_id."""

from __future__ import annotations

import pytest
from aegis.services.chat import ToolContext, _exec_find_reference


class _FakeKnowledge:
    """The knowledge service as `_exec_find_reference` uses it.

    #322: this test used to `patch("aegis.services.chat._exec_search_knowledge")`,
    a function the tool never calls — `patch` asserts the name EXISTS, never
    that anything reads it, so the line tested nothing and always would.
    The real collaborator is `ctx.knowledge_connector.search`, so drive that.
    """

    def __init__(self, results: list[dict]):
        self._results = results
        self.calls: list[dict] = []

    async def search(self, query, limit=10, source_type=None, **kwargs):
        self.calls.append({"query": query, "limit": limit, "source_type": source_type})
        return self._results


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

    knowledge = _FakeKnowledge([{"content_id": "K_REF_1", "title": "BCP runbook", "score": 0.81}])
    out = await _exec_find_reference(
        db_pool,
        {"query": "BCP", "limit": 10},
        ToolContext(agent_id="sebas", knowledge_connector=knowledge),
    )

    # The Todoist half selects on the label, never on the project.
    assert "T_BCP_REF" in out
    assert "T_OLD_REF" not in out
    # The knowledge half is asked for the REFERENCE corpus — an unfiltered
    # search would answer with every source type — and its hits reach the reply.
    assert knowledge.calls == [{"query": "BCP", "limit": 10, "source_type": "reference"}]
    assert "K_REF_1" in out
