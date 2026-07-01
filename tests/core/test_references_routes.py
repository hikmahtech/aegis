"""Tests for /api/references routes."""

import base64
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def mock_knowledge():
    kc = AsyncMock()
    # KS now filters server-side via source_type=reference; the connector mock
    # mirrors that contract — it returns only reference rows when the route
    # asks for them.
    kc.list_content_items.return_value = [
        {
            "content_id": "ref-1",
            "title": "Reference one",
            "source_type": "reference",
            "metadata": {"source_tag": "#research"},
            "created_at": "2026-05-21T00:00:00Z",
        },
        {
            "content_id": "ref-2",
            "title": "Reference from chat",
            "source_type": "reference",
            "metadata": {"source_tag": "#chat"},
            "created_at": "2026-05-21T01:00:00Z",
        },
    ]
    kc.search.return_value = [
        {"content_id": "ref-1", "title": "Search hit", "source_type": "reference"}
    ]
    kc.get_content_status.return_value = {
        "content_id": "ref-1",
        "title": "Reference one",
        "status": "ready",
        "chunks_total": 3,
    }
    kc.get_content_chunks.return_value = [
        {"id": 1, "index": 0, "text": "chunk 0"},
        {"id": 2, "index": 1, "text": "chunk 1"},
    ]
    return kc


def _make_pool_with_rows(rows: list[dict]):
    """Pool whose conn.fetch returns the given rows for any query."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    conn.fetchval = AsyncMock(return_value=None)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


@pytest.fixture
def app(test_settings, mock_knowledge):
    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: test_settings
    pool, _ = _make_pool_with_rows([])
    application.state.db_pool = pool
    application.state.knowledge_connector = mock_knowledge
    return application


@pytest.fixture
def auth_headers():
    creds = base64.b64encode(b"admin:admin").decode()
    return {"Authorization": f"Basic {creds}"}


async def test_list_references_filters_to_reference_source_type(
    app, auth_headers, mock_knowledge
):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/references", headers=auth_headers)
    assert resp.status_code == 200
    rows = resp.json()
    assert {r["content_id"] for r in rows} == {"ref-1", "ref-2"}
    # Route must push the source_type filter into KS rather than post-filter,
    # otherwise references below the recent-items cap are silently dropped.
    mock_knowledge.list_content_items.assert_awaited_once()
    kwargs = mock_knowledge.list_content_items.call_args.kwargs
    assert kwargs["source_type"] == "reference"
    assert kwargs["limit"] >= 500


async def test_list_references_source_tag_filter(app, auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/references", headers=auth_headers, params={"source_tag": "#chat"}
        )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["content_id"] == "ref-2"


async def test_list_references_source_tag_filter_falls_back_to_tags(
    test_settings, mock_knowledge, auth_headers
):
    """Legacy KS rows carry source_tag only in tags[] — filter must find them."""
    mock_knowledge.list_content_items = AsyncMock(
        return_value=[
            {
                "content_id": "legacy-1",
                "title": "Legacy reference (no metadata)",
                "source_type": "reference",
                "tags": ["gtd:reference", "#email"],
                # NOTE: no `metadata` field at all
            },
            {
                "content_id": "legacy-2",
                "title": "Another legacy reference",
                "source_type": "reference",
                "tags": ["gtd:reference", "#research"],
            },
        ]
    )
    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: test_settings
    pool, _ = _make_pool_with_rows([])
    application.state.db_pool = pool
    application.state.knowledge_connector = mock_knowledge
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/references", headers=auth_headers, params={"source_tag": "#email"}
        )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["content_id"] == "legacy-1"


async def test_list_references_search_query_uses_semantic(app, auth_headers, mock_knowledge):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/references", headers=auth_headers, params={"q": "kubernetes"}
        )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["title"] == "Search hit"
    mock_knowledge.search.assert_awaited_once_with(
        "kubernetes", limit=200, source_type="reference"
    )
    mock_knowledge.list_content_items.assert_not_called()


async def test_get_reference_returns_status_and_chunks(app, auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/references/ref-1", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"]["content_id"] == "ref-1"
    assert len(body["chunks"]) == 2


async def test_get_reference_translates_ks_404_to_404(
    test_settings, mock_knowledge, auth_headers
):
    request = httpx.Request("GET", "http://ks/api/content/missing/status")
    response = httpx.Response(404, request=request, json={"detail": "not found"})
    mock_knowledge.get_content_status = AsyncMock(
        side_effect=httpx.HTTPStatusError("404", request=request, response=response)
    )
    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: test_settings
    pool, _ = _make_pool_with_rows([])
    application.state.db_pool = pool
    application.state.knowledge_connector = mock_knowledge
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/references/missing", headers=auth_headers)
    assert resp.status_code == 404


async def test_list_failures_returns_demoted_tasks(test_settings, mock_knowledge, auth_headers):
    rows = [
        {
            "id": "TASK_A",
            "title": "Couldn't file this",
            "description": "https://dead.example.com/article",
            "labels": ["@to-read", "@raphael"],
            "source_tag": "#research",
            "updated_at": None,
            "last_clarified_at": None,
            "demotion_note": (
                "[ClarifyFlow @ ref-demote] couldn't file in knowledge service "
                "— http_404: not found. Demoted to @to-read."
            ),
        }
    ]
    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: test_settings
    pool, _ = _make_pool_with_rows(rows)
    application.state.db_pool = pool
    application.state.knowledge_connector = mock_knowledge
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/references/failures", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == "TASK_A"
    assert "@to-read" in body[0]["labels"]
    assert "http_404" in body[0]["demotion_note"]


async def test_list_references_returns_503_when_ks_missing(test_settings, auth_headers):
    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: test_settings
    pool, _ = _make_pool_with_rows([])
    application.state.db_pool = pool
    application.state.knowledge_connector = None
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/references", headers=auth_headers)
    assert resp.status_code == 503
