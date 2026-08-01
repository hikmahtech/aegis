"""Tests for POST /api/admin/capture (chat /capture backend)."""

from __future__ import annotations

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.services.knowledge import KnowledgeStore
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

_TEST_REQUIRED_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "n8n_ui_url": "https://n8n.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
}


@pytest.fixture
def settings():
    return Settings(**_TEST_REQUIRED_SETTINGS)


@pytest_asyncio.fixture(loop_scope="function")
async def app_client(settings, db_pool):
    """FastAPI client with verify_auth + get_settings overridden."""
    app = create_app(run_lifespan=False)
    app.state.db_pool = db_pool
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[verify_auth] = lambda: None
    return TestClient(app)


def test_capture_route_returns_task_ref(app_client, monkeypatch):
    """When _capture_to_inbox_impl returns a ref, the route returns it."""
    async def fake_capture(pool, source_tag, external_id, title, description):
        return "TASK-CAPTURE-1"

    monkeypatch.setattr(
        "aegis.services.chat._capture_to_inbox_impl", fake_capture, raising=False
    )
    r = app_client.post(
        "/api/admin/capture",
        json={"text": "buy milk", "source": "chat"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_ref"] == "TASK-CAPTURE-1"
    assert body["source_tag"] == "#chat"
    # Default external_id is hash-based
    assert body["external_id"].startswith("chat:")


def test_capture_route_honors_explicit_external_id(app_client, monkeypatch):
    captured: dict = {}

    async def fake_capture(pool, source_tag, external_id, title, description):
        captured["ext_id"] = external_id
        return "TASK-CAPTURE-2"

    monkeypatch.setattr(
        "aegis.services.chat._capture_to_inbox_impl", fake_capture, raising=False
    )
    r = app_client.post(
        "/api/admin/capture",
        json={"text": "x", "source": "chat", "external_id": "chat:12345:abc"},
    )
    assert r.status_code == 200
    assert captured["ext_id"] == "chat:12345:abc"


def test_capture_route_503_when_impl_returns_none(app_client, monkeypatch):
    """Kill-switch off / missing inbox → 503."""
    async def fake_capture(*a, **kw):
        return None

    monkeypatch.setattr(
        "aegis.services.chat._capture_to_inbox_impl", fake_capture, raising=False
    )
    r = app_client.post("/api/admin/capture", json={"text": "x"})
    assert r.status_code == 503
    assert "capture skipped" in r.text


def test_capture_route_rejects_empty_text(app_client):
    r = app_client.post("/api/admin/capture", json={"text": ""})
    assert r.status_code == 422  # pydantic min_length=1


def test_capture_route_rejects_bad_source(app_client):
    r = app_client.post(
        "/api/admin/capture",
        json={"text": "x", "source": "WITH-DASH"},  # uppercase + dash not allowed
    )
    assert r.status_code == 422


# --- kind="life_fact" lane (B1) --------------------------------------------

_DIM = 768


class _FakeLLM:
    """Deterministic embeddings so ingest_content exercises real SQL."""

    async def embed(self, texts, model="nomic-embed-text"):
        vecs = []
        for t in texts:
            v = [0.0] * _DIM
            v[sum(ord(c) for c in t) % _DIM] = 1.0
            vecs.append(v)
        return vecs


@pytest_asyncio.fixture(loop_scope="function")
async def life_fact_client(settings, db_pool):
    """Async client with a real pgvector KnowledgeStore wired in.

    httpx + ASGITransport (not TestClient) because these requests touch the
    asyncpg pool, which is bound to this test's event loop.
    """
    app = create_app(run_lifespan=False)
    app.state.db_pool = db_pool
    app.state.knowledge_connector = KnowledgeStore(
        db_pool=db_pool, llm=_FakeLLM(), embedding_model="nomic-embed-text"
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[verify_auth] = lambda: None
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client
    await db_pool.execute(
        "DELETE FROM knowledge_content WHERE url LIKE 'aegis://life_fact/%'"
    )


async def test_capture_life_fact_writes_knowledge_row(life_fact_client, db_pool):
    """kind=life_fact → 200 + a knowledge_content row, no Todoist task_ref."""
    r = await life_fact_client.post(
        "/api/admin/capture",
        json={
            "text": "my passport expires in March 2030",
            "source": "slack",
            "kind": "life_fact",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_ref"] is None
    content_id = body["content_id"]
    assert content_id

    row = await db_pool.fetchrow(
        "SELECT url, title, source_type, tags FROM knowledge_content WHERE content_id = $1",
        content_id,
    )
    assert row is not None, "no knowledge_content row written for the life fact"
    assert row["source_type"] == "life_fact"
    assert row["url"] == f"aegis://life_fact/{body['external_id']}"
    assert row["title"] == "my passport expires in March 2030"
    assert set(row["tags"]) == {"life_fact", "slack"}


async def test_capture_life_fact_reposting_is_idempotent(life_fact_client, db_pool):
    """Identical text → same content_id, exactly one row."""
    payload = {
        "text": "the boiler was serviced in June",
        "source": "chat",
        "kind": "life_fact",
    }
    first = await life_fact_client.post("/api/admin/capture", json=payload)
    second = await life_fact_client.post("/api/admin/capture", json=payload)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    cid = first.json()["content_id"]
    assert second.json()["content_id"] == cid

    count = await db_pool.fetchval(
        "SELECT count(*) FROM knowledge_content WHERE content_id = $1", cid
    )
    assert count == 1


async def test_capture_life_fact_does_not_touch_todoist(life_fact_client, monkeypatch):
    """The life_fact lane must never call the Todoist capture impl."""
    called: list[str] = []

    async def fake_capture(*a, **kw):
        called.append("yes")
        return "TASK-SHOULD-NOT-HAPPEN"

    monkeypatch.setattr(
        "aegis.services.chat._capture_to_inbox_impl", fake_capture, raising=False
    )
    r = await life_fact_client.post(
        "/api/admin/capture",
        json={"text": "I switched dentists", "source": "chat", "kind": "life_fact"},
    )
    assert r.status_code == 200, r.text
    assert called == [], "_capture_to_inbox_impl was called on the life_fact lane"
    assert r.json()["task_ref"] is None


async def test_capture_life_fact_blank_text_is_rejected(life_fact_client, db_pool):
    """Whitespace-only text writes no row — the caller must not get an id.

    pydantic's min_length=1 lets "   " through, the route strips it, and
    ingest_content early-returns status="empty" without writing.
    """
    r = await life_fact_client.post(
        "/api/admin/capture",
        json={"text": "   ", "source": "chat", "kind": "life_fact"},
    )

    assert r.status_code == 422, r.text
    assert "content_id" not in r.text
    rows = await db_pool.fetchval(
        "SELECT count(*) FROM knowledge_content WHERE url LIKE 'aegis://life_fact/%'"
    )
    assert rows == 0, "a row was written for blank life_fact text"


def test_capture_default_kind_is_task(app_client, monkeypatch):
    """kind omitted → the unchanged Todoist path (no behaviour drift)."""
    async def fake_capture(pool, source_tag, external_id, title, description):
        return "TASK-DEFAULT"

    monkeypatch.setattr(
        "aegis.services.chat._capture_to_inbox_impl", fake_capture, raising=False
    )
    r = app_client.post("/api/admin/capture", json={"text": "call the plumber"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_ref"] == "TASK-DEFAULT"
    assert body["content_id"] is None


def test_capture_rejects_unknown_kind(app_client, monkeypatch):
    """An unknown lane is a 422, not silently dropped into the task path."""
    async def fake_capture(pool, source_tag, external_id, title, description):
        return "TASK-SHOULD-NOT-HAPPEN"

    monkeypatch.setattr(
        "aegis.services.chat._capture_to_inbox_impl", fake_capture, raising=False
    )
    r = app_client.post(
        "/api/admin/capture", json={"text": "x", "kind": "not_a_lane"}
    )
    assert r.status_code == 422, r.text
    assert "kind" in r.text
