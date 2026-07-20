"""Real-Postgres tests for the native pgvector KnowledgeStore.

Uses the `db_pool` fixture (local dev Postgres, pgvector image). A fake LLM
supplies deterministic 768-dim embeddings so the SQL/plumbing is exercised
without a live embedding model.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from aegis.db import run_migrations
from aegis.services.knowledge import KnowledgeStore

pytestmark = pytest.mark.asyncio

_DIM = 768


def _vec_for(text: str) -> list[float]:
    """Deterministic non-zero unit-ish vector keyed on text content."""
    v = [0.0] * _DIM
    v[sum(ord(c) for c in text) % _DIM] = 1.0
    v[len(text) % _DIM] += 0.5
    return v


class _FakeLLM:
    async def embed(self, texts, model="nomic-embed-text"):
        return [_vec_for(t) for t in texts]

    async def think(self, prompt, **kwargs):
        return {"response": "synthesized answer [1]", "model": "fake",
                "prompt_tokens": 0, "completion_tokens": 0}


@pytest_asyncio.fixture(loop_scope="function")
async def store(db_pool):
    await run_migrations(db_pool, "migrations")
    s = KnowledgeStore(db_pool=db_pool, llm=_FakeLLM(), embedding_model="nomic-embed-text")
    yield s
    # Clean up rows this test created.
    await db_pool.execute(
        "DELETE FROM knowledge_content WHERE url LIKE 'aegis://test/%'"
    )


async def test_ingest_search_and_admin_roundtrip(store, db_pool):
    url = "aegis://test/alpha"
    res = await store.ingest_content(
        url=url,
        title="Alpha Doc",
        source_type="article",
        raw_text="alpha beta gamma delta " * 200,  # forces multiple chunks
        tags=["topic-x"],
        metadata={"significance": 4},
    )
    assert res["status"] == "ok"
    assert res["chunks_total"] >= 2
    cid = res["content_id"]

    # search returns the doc with the expected shape
    hits = await store.search("alpha beta", limit=5)
    assert any(h["content_id"] == cid for h in hits)
    hit = next(h for h in hits if h["content_id"] == cid)
    assert hit["id"] == cid
    assert hit["title"] == "Alpha Doc"
    assert "similarity" in hit and hit["content"]
    assert hit["metadata"]["significance"] == 4
    assert isinstance(hit["ingested_at"], str)  # serialized for callers

    # source_type filter narrows
    assert await store.search("alpha", limit=5, source_type="article")
    assert await store.search("alpha", limit=5, source_type="nonexistent") == []

    # admin reads
    items = await store.list_content_items(limit=50, source_type="article")
    item = next(it for it in items if it["content_id"] == cid)
    assert item["chunks_total"] >= 2

    status = await store.get_content_status(cid)
    assert status["status"] == "completed"
    assert (await store.get_content_status("does-not-exist"))["status"] == "not_found"

    chunks = await store.get_content_chunks(cid)
    assert len(chunks) >= 2 and chunks[0]["chunk_index"] == 0

    stats = await store.get_stats()
    assert stats["content_items"] >= 1

    # ask() = search + LLM synthesis over local LLM
    answer = await store.ask("what is alpha?", max_sources=3)
    assert answer["answer"] and answer["sources"]


async def test_reingest_same_url_replaces_chunks(store):
    url = "aegis://test/replace"
    first = await store.ingest_content(url=url, title="v1", source_type="note",
                                       raw_text="one two three " * 200)
    second = await store.ingest_content(url=url, title="v2", source_type="note",
                                        raw_text="short body")
    assert first["content_id"] == second["content_id"]
    chunks = await store.get_content_chunks(second["content_id"])
    assert len(chunks) == 1  # replaced, not appended
    status = await store.get_content_status(second["content_id"])
    assert status["title"] == "v2"


async def test_empty_body_is_skipped(store):
    res = await store.ingest_content(url="aegis://test/empty", title="", source_type="x")
    assert res["status"] == "empty" and res["chunks_total"] == 0


async def test_nul_bytes_in_extracted_text_are_stripped(store):
    """PDF/other extractors can emit NUL bytes; Postgres rejects them outright
    (`invalid byte sequence for encoding "UTF8": 0x00`). Confirms ingest_content
    sanitizes title/raw_text before the INSERT instead of the write failing."""
    url = "aegis://test/nul-bytes"
    res = await store.ingest_content(
        url=url,
        title="Doc\x00 With NUL",
        source_type="article",
        raw_text="alpha\x00beta\x00 gamma delta",
    )
    assert res["status"] == "ok"
    cid = res["content_id"]

    chunks = await store.get_content_chunks(cid)
    assert chunks and "\x00" not in chunks[0]["chunk_text"]
    assert chunks[0]["chunk_text"] == "alphabeta gamma delta"

    status = await store.get_content_status(cid)
    assert status["title"] == "Doc With NUL"
