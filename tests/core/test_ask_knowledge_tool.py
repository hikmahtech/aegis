"""`ask_knowledge` as a chat tool (#662): filters, the caller's ranking and
agent id reach `KnowledgeStore.ask`; an empty answer says so; the admin
`POST /api/knowledge/ask` route still asks exactly as it did."""

from __future__ import annotations

import json

import httpx
import pytest
from aegis.api.auth import verify_auth
from aegis.api.routes import knowledge as kroute
from aegis.services.knowledge_ranking import Ranking
from aegis.services.tools.base import ToolContext
from aegis.services.tools.knowledge import _exec_ask_knowledge
from fastapi import FastAPI

pytestmark = pytest.mark.asyncio


class _RecordingStore:
    def __init__(self, sources=None):
        self.calls: list[tuple[tuple, dict]] = []
        self._sources = [{"title": "T", "url": "u", "similarity": 0.9}] if sources is None else sources

    async def ask(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if not self._sources:
            return {"answer": "", "sources": [], "confidence": 0.0}
        return {"answer": "A [1]", "sources": self._sources, "confidence": 0.9}


async def test_tool_passes_filters_ranking_domains_and_agent_through(db_pool):
    store = _RecordingStore()
    ctx = ToolContext(agent_id="raphael", knowledge_connector=store)

    raw = await _exec_ask_knowledge(
        db_pool,
        {"question": "what did the bank say?", "source_type": "email", "tags": ["bank"], "since_days": 7},
        ctx,
    )

    out = json.loads(raw)
    assert out["answered"] is True and out["answer"] == "A [1]"
    (args, kwargs), = store.calls
    assert args == ("what did the bank say?",)
    assert kwargs["source_type"] == "email"
    assert kwargs["tags"] == ["bank"]
    assert kwargs["since_days"] == 7
    assert kwargs["agent_id"] == "raphael"
    assert isinstance(kwargs["ranking"], Ranking)
    meta = await db_pool.fetchval("SELECT metadata FROM agents WHERE id = 'raphael'")
    assert kwargs["knowledge_domains"] == meta["knowledge_domains"]
    assert kwargs["knowledge_domains"]  # the seed gives raphael domains


async def test_tool_without_an_agent_asks_with_no_domains(db_pool):
    store = _RecordingStore()
    await _exec_ask_knowledge(db_pool, {"question": "q"}, ToolContext(knowledge_connector=store))
    (_, kwargs), = store.calls
    assert kwargs["agent_id"] is None
    assert kwargs["knowledge_domains"] is None
    assert kwargs["source_type"] is None and kwargs["tags"] is None and kwargs["since_days"] is None


async def test_tool_says_plainly_when_nothing_matched(db_pool):
    store = _RecordingStore(sources=[])
    out = json.loads(await _exec_ask_knowledge(db_pool, {"question": "q"}, ToolContext(knowledge_connector=store)))
    assert out["answered"] is False
    assert out["sources"] == []
    assert "no documents" in out["answer"]


async def test_admin_ask_route_is_unchanged():
    app = FastAPI()
    app.include_router(kroute.router)
    app.dependency_overrides[verify_auth] = lambda: True
    store = _RecordingStore()
    app.state.knowledge_connector = store
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/knowledge/ask", json={"question": "q", "max_sources": 3, "min_confidence": 0.2})
    assert r.status_code == 200
    assert r.json() == {"answer": "A [1]", "sources": store._sources, "confidence": 0.9}
    assert store.calls == [(("q",), {"max_sources": 3, "min_confidence": 0.2})]
