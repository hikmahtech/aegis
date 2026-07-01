"""Phase 3: per-agent routing is data-driven from agents.metadata (overrides the
shipped defaults). Proves a NEW agent works without editing the hardcoded dicts."""

from __future__ import annotations

import pytest_asyncio
from aegis.services.chat import (
    _agent_keyword_map,
    _get_agent_tools,
    _keyword_route,
    classify_intent,
)


def test_tool_set_from_metadata_overrides_defaults():
    # A custom agent not in AGENT_TOOL_SETS, with its own metadata.tool_set.
    tools = _get_agent_tools("brand-new-agent", metadata={"tool_set": ["search_knowledge"]})
    names = {t["function"]["name"] for t in tools}
    assert names == {"search_knowledge"}


def test_tool_set_falls_back_to_default_when_no_metadata():
    # No metadata → shipped default for a known agent.
    raphael = {t["function"]["name"] for t in _get_agent_tools("raphael")}
    assert "ask_knowledge" in raphael  # raphael's default set


def test_keyword_route_uses_provided_map():
    assert _keyword_route("deploy the thing", {"ops": ["deploy"], "x": ["nope"]}) == "ops"
    assert _keyword_route("nothing matches", {"ops": ["deploy"]}) is None


@pytest_asyncio.fixture(loop_scope="function")
async def seeded_agent(db_pool):
    await db_pool.execute("DELETE FROM agents WHERE id = 'zztest-agent'")
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, active, metadata) "
        "VALUES ('zztest-agent','Z','tester','', true, "
        "'{\"intent_keywords\": [\"frobnicate\"]}'::jsonb)"
    )
    yield
    await db_pool.execute("DELETE FROM agents WHERE id = 'zztest-agent'")


async def test_keyword_map_built_from_db_metadata(db_pool, seeded_agent):
    kmap = await _agent_keyword_map(db_pool)
    assert kmap.get("zztest-agent") == ["frobnicate"]
    # and routing picks it up
    out = await classify_intent("please frobnicate this", llm=None, settings=None, pool=db_pool)
    assert out["agent_id"] == "zztest-agent"
