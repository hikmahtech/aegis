"""Issue #36 #5/#6 — the LLM intent-router prompt is built from active agents'
metadata.intent_description (data-driven), so a renamed/added agent is reachable
via LLM routing, not just keyword/@mention. Since #556 the rows are the ONLY
source: there are no shipped descriptions behind them.
"""

from __future__ import annotations

import pytest_asyncio
from aegis.services.chat import (
    _agent_intent_descriptions,
    _build_intent_prompt,
    _generalists_of,
    _routing_agents,
    classify_intent,
)


async def test_intent_prompt_lists_seeded_agents_generalist_last(db_pool):
    """The seeded agents' own descriptions, specific agents first and the `gtd`
    holder last — the order the old hardcoded precedence list gave."""
    agents = await _routing_agents(db_pool)
    descriptions = await _agent_intent_descriptions(db_pool)
    prompt = _build_intent_prompt("hello", descriptions, _generalists_of(agents))
    for aid in ("maou", "pandoras-actor", "raphael", "sebas"):
        assert f"- {aid}: {descriptions[aid]}" in prompt
    order = [prompt.index(f"- {aid}:") for aid in ("maou", "pandoras-actor", "raphael", "sebas")]
    assert order == sorted(order)


def test_intent_prompt_includes_custom_agent():
    """A custom agent with an intent_description is offered to the router."""
    descriptions = {"jeeves": "butler duties, scheduling, reminders", "sebas": "general"}
    prompt = _build_intent_prompt("hi", descriptions)
    assert "- jeeves: butler duties, scheduling, reminders" in prompt


class _StubLLM:
    """Minimal LLM stub that always routes to a fixed agent id."""

    def __init__(self, agent_id: str):
        self._agent_id = agent_id

    async def think(self, prompt, model=None, max_tokens=None, purpose=None):
        return {"response": f'{{"agent_id": "{self._agent_id}", "reason": "test"}}'}


@pytest_asyncio.fixture(loop_scope="function")
async def custom_research_agent(db_pool):
    # Reachable via intent_description ONLY (no intent_keywords), to prove the
    # LLM-router acceptance no longer requires a keyword-map entry.
    await db_pool.execute("DELETE FROM agents WHERE id = 'tagtest-jeeves'")
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, active, metadata) "
        "VALUES ('tagtest-jeeves','Jeeves','butler','', true, "
        '\'{"intent_description": "butler duties and scheduling"}\'::jsonb)'
    )
    yield
    await db_pool.execute("DELETE FROM agents WHERE id = 'tagtest-jeeves'")


async def test_intent_descriptions_built_from_db(db_pool, custom_research_agent):
    descriptions = await _agent_intent_descriptions(db_pool)
    assert descriptions.get("tagtest-jeeves") == "butler duties and scheduling"
    # virtual 'system' agent (no description) is omitted
    assert "system" not in descriptions


async def test_llm_route_accepts_description_only_agent(db_pool, custom_research_agent):
    """LLM names a custom agent reachable only via intent_description — accepted
    (previously rejected because it wasn't in the keyword map)."""
    out = await classify_intent(
        "please draw my bath",
        llm=_StubLLM("tagtest-jeeves"),
        settings=None,
        pool=db_pool,
    )
    assert out["agent_id"] == "tagtest-jeeves"
    assert out["method"] == "llm"


async def test_intent_descriptions_empty_without_pool():
    """No rows, no descriptions — never the example agents' names (#556)."""
    assert await _agent_intent_descriptions(None) == {}
