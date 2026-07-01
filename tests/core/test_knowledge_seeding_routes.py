"""Route tests for knowledge seeding (upload / folder / url fetch+extract).

Uses a minimal app with a fake KnowledgeStore on app.state so no DB/embeddings
are needed — the routes only need `.ingest_content(...)`.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from aegis.api.auth import verify_auth
from aegis.api.routes import knowledge as kroute
from fastapi import FastAPI

pytestmark = pytest.mark.asyncio


class _FakeStore:
    def __init__(self):
        self.calls: list[dict] = []

    async def ingest_content(self, **kw):
        self.calls.append(kw)
        return {"content_id": "cid", "status": "ok", "chunks_total": 2}


@pytest.fixture
def client_store():
    app = FastAPI()
    app.include_router(kroute.router)
    app.dependency_overrides[verify_auth] = lambda: True
    store = _FakeStore()
    app.state.knowledge_connector = store
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://t"), store


async def test_upload_extracts_and_ingests(client_store):
    client, store = client_store
    async with client:
        r = await client.post(
            "/api/knowledge/upload",
            files={"file": ("notes.md", b"alpha body text", "text/markdown")},
            data={"source_type": "upload", "tags": "x, y"},
        )
    assert r.status_code == 200
    call = store.calls[0]
    assert "alpha body text" in call["raw_text"]
    assert call["tags"] == ["x", "y"]
    assert call["url"] == "upload://notes.md"


async def test_ingest_folder_walks_and_filters(tmp_path, client_store):
    (tmp_path / "a.md").write_text("first doc")
    (tmp_path / "b.txt").write_text("second doc")
    (tmp_path / "skip.bin").write_bytes(b"\x00\x01")
    client, store = client_store
    async with client:
        r = await client.post("/api/knowledge/ingest-folder", json={"path": str(tmp_path)})
    assert r.status_code == 200
    body = r.json()
    assert body["ingested"] == 2
    assert body["skipped"] >= 1  # the .bin
    assert len(store.calls) == 2


async def test_ingest_url_fetches_and_extracts(client_store):
    client, store = client_store
    html = "<html><head><title>T</title></head><body><article><p>" + ("real sentence. " * 30) + "</p></article></body></html>"
    with respx.mock:
        respx.get("http://x/article").mock(
            return_value=httpx.Response(200, headers={"content-type": "text/html"}, text=html)
        )
        async with client:
            r = await client.post("/api/knowledge/ingest", json={"url": "http://x/article"})
    assert r.status_code == 200
    assert store.calls[0]["raw_text"]  # extracted, not None
    assert "real sentence" in store.calls[0]["raw_text"]


async def test_ingest_folder_rejects_non_directory(client_store):
    client, _ = client_store
    async with client:
        r = await client.post("/api/knowledge/ingest-folder", json={"path": "/no/such/dir"})
    assert r.status_code == 400
