# tests/core/test_intent_route.py
"""Intent classifier (_keyword_route / classify_intent) + POST /api/chat/route."""
from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.services.chat import _keyword_route, classify_intent
from httpx import ASGITransport, AsyncClient


def test_keyword_route_picks_domain_agent():
    assert _keyword_route("what's my AWS bill this month") == "maou"
    assert _keyword_route("restart the docker swarm node") == "pandoras-actor"
    assert _keyword_route("summarize what we know about X") == "raphael"
    assert _keyword_route("add a task to my inbox for tomorrow") == "sebas"


def test_keyword_route_none_when_no_keyword():
    assert _keyword_route("tell me a joke") is None


@pytest.mark.asyncio
async def test_classify_intent_keyword_skips_llm():
    llm = AsyncMock()
    out = await classify_intent("pay the electricity bill", llm, None)
    assert out["agent_id"] == "maou"
    assert out["method"] == "keyword"
    llm.think.assert_not_called()


@pytest.mark.asyncio
async def test_classify_intent_llm_fallback():
    llm = AsyncMock()
    llm.think = AsyncMock(return_value={"response": '{"agent_id": "raphael", "reason": "research"}'})
    out = await classify_intent("ponder the nature of the thing", llm, None)
    assert out["agent_id"] == "raphael"
    assert out["method"] == "llm"


@pytest.mark.asyncio
async def test_classify_intent_defaults_sebas_on_llm_error():
    llm = AsyncMock()
    llm.think = AsyncMock(side_effect=RuntimeError("proxy down"))
    out = await classify_intent("ponder the nature of the thing", llm, None)
    assert out["agent_id"] == "sebas"
    assert out["method"] == "default"


@pytest.mark.asyncio
async def test_classify_intent_no_llm_defaults_sebas():
    out = await classify_intent("ponder the nature of the thing", None, None)
    assert out["agent_id"] == "sebas"
    assert out["method"] == "default"


@pytest.fixture
def app(test_settings, mock_db_pool):
    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: test_settings
    application.state.db_pool = mock_db_pool
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
