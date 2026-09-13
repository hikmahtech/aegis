"""A fork that renamed its agents routes exactly like the example set (#556).

Before #556 the front door carried keyword, description and default maps keyed
on the four example ids. A deployment whose GTD agent is called something else
lost the "default to the generalist" rule and fell back to `sebas`, an agent it
did not have. These tests rename the GTD agent in the database and check that
everything follows the row and the `gtd` tag.
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

RENAMED = "tagtest-jarvis"


@pytest_asyncio.fixture(loop_scope="function")
async def renamed(db_pool):
    """`sebas` switched off and replaced by an agent with his metadata and tag."""
    await db_pool.execute("DELETE FROM agents WHERE id = $1", RENAMED)
    await db_pool.execute(
        """
        INSERT INTO agents (id, name, role, system_prompt_path, capabilities, model_tier,
                            metadata, active)
        SELECT $1, 'Jarvis', role, '', capabilities, model_tier, metadata, TRUE
        FROM agents WHERE id = 'sebas'
        """,
        RENAMED,
    )
    await db_pool.execute("UPDATE agents SET active = FALSE WHERE id = 'sebas'")
    try:
        yield db_pool
    finally:
        await db_pool.execute("UPDATE agents SET active = TRUE WHERE id = 'sebas'")
        await db_pool.execute("DELETE FROM agents WHERE id = $1", RENAMED)


async def test_keywords_route_to_the_renamed_agent(renamed):
    out = await classify_intent("add a task to my inbox for tomorrow", None, None, pool=renamed)
    assert out == {"agent_id": RENAMED, "reason": "keyword", "method": "keyword"}


async def test_the_default_is_the_renamed_gtd_agent(renamed):
    out = await classify_intent("ponder the nature of the thing", None, None, pool=renamed)
    assert out["agent_id"] == RENAMED and out["method"] == "default"


async def test_the_renamed_generalist_is_listed_last_to_the_llm(renamed):
    agents = await _routing_agents(renamed)
    prompt = _build_intent_prompt(
        "hi", await _agent_intent_descriptions(renamed), _generalists_of(agents)
    )
    assert "- sebas:" not in prompt
    assert prompt.index("- raphael:") < prompt.index(f"- {RENAMED}:")


@pytest_asyncio.fixture(loop_scope="function")
async def no_generalist(db_pool):
    await db_pool.execute("UPDATE agents SET active = FALSE WHERE id = 'sebas'")
    try:
        yield db_pool
    finally:
        await db_pool.execute("UPDATE agents SET active = TRUE WHERE id = 'sebas'")


async def test_with_no_gtd_holder_there_is_no_default(no_generalist):
    """Zero holders: the specific agents still route, and the default is ""
    (comms then uses its channel's agent) — never a guessed id."""
    out = await classify_intent("ponder the nature of the thing", None, None, pool=no_generalist)
    assert out == {"agent_id": "", "reason": "no_llm", "method": "default"}
    kw = await classify_intent("pay the electricity bill", None, None, pool=no_generalist)
    assert kw["agent_id"] == "maou"
