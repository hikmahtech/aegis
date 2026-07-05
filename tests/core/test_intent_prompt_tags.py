"""Issue #36 #5/#6 — the LLM intent-router prompt is built from active agents'
metadata.intent_description (data-driven), so a renamed/added agent is reachable
via LLM routing, not just keyword/@mention. Shipped descriptions are the fallback.
"""

from __future__ import annotations

import pytest_asyncio
from aegis.services.chat import (
    _agent_intent_descriptions,
    _build_intent_prompt,
    classify_intent,
)


def test_intent_prompt_fallback_lists_seed_agents():
    """With no descriptions, the prompt lists the 4 seed agents in precedence
    order (byte-identical to the shipped prompt)."""
    prompt = _build_intent_prompt("hello")
    assert "- maou: finance, money, subscriptions, receipts, market" in prompt
    assert "- pandoras-actor: infrastructure, servers, deploys, homelab, logs" in prompt
    assert "- raphael: research, knowledge, learning, summarizing" in prompt
    assert "- sebas: tasks, GTD, calendar, email, general (the default)" in prompt
    # precedence order preserved
    assert prompt.index("- maou:") < prompt.index("- sebas:")


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


async def test_intent_descriptions_fallback_without_pool():
    descriptions = await _agent_intent_descriptions(None)
    assert set(descriptions) == {"maou", "pandoras-actor", "raphael", "sebas"}
