# tests/core/test_intent_route.py
"""Intent classifier (_keyword_route / classify_intent) + POST /api/chat/route.

Routing reads the active agents' rows and nothing else (#556), so these run
against the seeded agents — the metadata `config/seed/agents.yaml` gives a
deployment on boot. The expected agents are the ones the old hardcoded maps
produced: same messages, same answers, now from the database.
"""
from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.services.chat import (
    _generalists_of,
    _keyword_route,
    _keywords_of,
    _routing_agents,
    classify_intent,
)
from httpx import ASGITransport, AsyncClient


async def _seeded(db_pool):
    agents = await _routing_agents(db_pool)
    return _keywords_of(agents), _generalists_of(agents)


async def test_keyword_route_picks_domain_agent(db_pool):
    kmap, gens = await _seeded(db_pool)
    assert _keyword_route("what's my AWS bill this month", kmap, gens) == "maou"
    assert _keyword_route("restart the docker swarm node", kmap, gens) == "pandoras-actor"
    assert _keyword_route("summarize what we know about X", kmap, gens) == "raphael"
    assert _keyword_route("add a task to my inbox for tomorrow", kmap, gens) == "sebas"


async def test_keyword_route_none_when_no_keyword(db_pool):
    kmap, gens = await _seeded(db_pool)
    assert _keyword_route("tell me a joke", kmap, gens) is None
    # No agents known → nothing to route to.
    assert _keyword_route("pay the bill") is None


def test_keyword_tie_goes_to_the_specific_agent_not_the_generalist():
    """`gtd` holders tie-break last, whatever their id sorts as."""
    kmap = {"aaa-general": ["schedule"], "zzz-infra": ["schedule"]}
    assert _keyword_route("schedule it", kmap, {"aaa-general"}) == "zzz-infra"
    # Without the tag, plain id order decides.
    assert _keyword_route("schedule it", kmap) == "aaa-general"


@pytest.mark.asyncio
async def test_classify_intent_keyword_skips_llm(db_pool):
    llm = AsyncMock()
    out = await classify_intent("pay the electricity bill", llm, None, pool=db_pool)
    assert out["agent_id"] == "maou"
    assert out["method"] == "keyword"
    llm.think.assert_not_called()


@pytest.mark.asyncio
async def test_classify_intent_llm_fallback(db_pool):
    llm = AsyncMock()
    llm.think = AsyncMock(return_value={"response": '{"agent_id": "raphael", "reason": "research"}'})
    out = await classify_intent("ponder the nature of the thing", llm, None, pool=db_pool)
    assert out["agent_id"] == "raphael"
    assert out["method"] == "llm"


@pytest.mark.asyncio
async def test_classify_intent_defaults_to_the_gtd_agent_on_llm_error(db_pool):
    llm = AsyncMock()
    llm.think = AsyncMock(side_effect=RuntimeError("proxy down"))
    out = await classify_intent("ponder the nature of the thing", llm, None, pool=db_pool)
    assert out["agent_id"] == "sebas"
    assert out["method"] == "default"


@pytest.mark.asyncio
async def test_classify_intent_no_llm_defaults_to_the_gtd_agent(db_pool):
    out = await classify_intent("ponder the nature of the thing", None, None, pool=db_pool)
    assert out["agent_id"] == "sebas"
    assert out["method"] == "default"


@pytest.mark.asyncio
async def test_classify_intent_without_a_pool_names_no_agent():
    """No rows, no default: "" makes comms fall back to its channel's agent
    rather than to an example id a fork may not have."""
    out = await classify_intent("ponder the nature of the thing", None, None)
    assert out == {"agent_id": "", "reason": "no_llm", "method": "default"}


@pytest.fixture
def app(test_settings, db_pool):
    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: test_settings
    application.state.db_pool = db_pool
    application.state.llm = None  # keyword path; no LLM needed
    application.state.settings = test_settings
    return application


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Basic {base64.b64encode(b'admin:admin').decode()}"}


@pytest.mark.asyncio
async def test_route_endpoint_keyword(app, auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/chat/route", headers=auth_headers,
                                 json={"message": "cancel my subscription invoice"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["agent_id"] == "maou"


@pytest.mark.asyncio
async def test_route_endpoint_empty_message_400(app, auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/chat/route", headers=auth_headers, json={"message": "  "})
    assert resp.status_code == 400
