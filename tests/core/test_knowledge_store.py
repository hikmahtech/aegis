"""Real-Postgres tests for the native pgvector KnowledgeStore.

Uses the `db_pool` fixture (local dev Postgres, pgvector image). A fake LLM
supplies deterministic 768-dim embeddings so the SQL/plumbing is exercised
without a live embedding model.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from aegis.db import run_migrations
from aegis.services.knowledge import KnowledgeStore, _vec_literal

pytestmark = pytest.mark.asyncio

_DIM = 768


def _vec_for(text: str) -> list[float]:
    """Deterministic non-zero unit-ish vector keyed on text content."""
    v = [0.0] * _DIM
    v[sum(ord(c) for c in text) % _DIM] = 1.0
    v[len(text) % _DIM] += 0.5
    return v


def _unit_at(idx: int, weight: float = 1.0, dim: int = _DIM) -> list[float]:
    """Basis-ish vector with a single non-zero component, for hand-crafting
    exact/known cosine similarities against a query embedding."""
    v = [0.0] * dim
    v[idx] = weight
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


async def _insert_content(db_pool, content_id, url, title, source_type, tags):
    await db_pool.execute(
        """
        INSERT INTO knowledge_content (content_id, url, title, source_type, tags, metadata)
        VALUES ($1, $2, $3, $4, $5, '{}'::jsonb)
        """,
        content_id, url, title, source_type, list(tags),
    )


async def _insert_chunk(db_pool, content_id, chunk_index, chunk_text, embedding):
    await db_pool.execute(
        """
        INSERT INTO knowledge_chunks (content_id, chunk_index, chunk_text, embedding)
        VALUES ($1, $2, $3, $4::vector)
        """,
        content_id, chunk_index, chunk_text, _vec_literal(embedding),
    )


async def test_search_dedupes_nearest_chunk_orders_by_similarity_and_filters(store, db_pool):
    """Locks in the read-path semantics the index-scan rewrite (oversampled
    ANN candidate stage + outer join/filter/dedupe) must preserve: exactly
    one row per content_id — the NEAREST chunk, not just any chunk — overall
    results ordered by similarity desc, and source_type/tags filters still
    narrow correctly. Embeddings are hand-crafted (not the fake LLM's
    hash-based vectors) so the expected similarities are exact, not merely
    directionally plausible.
    """
    query = "Q"
    qvec = _vec_for(query)  # non-zero only at idx 81 (=1.0) and idx 1 (+=0.5)

    near_id, mid_id, far_id = "test-dedup-near", "test-dedup-mid", "test-dedup-far"
    await _insert_content(db_pool, near_id, "aegis://test/dedup-near", "Near", "article", ["t1"])
    await _insert_content(db_pool, mid_id, "aegis://test/dedup-mid", "Mid", "reportX", ["special-tag"])
    await _insert_content(db_pool, far_id, "aegis://test/dedup-far", "Far", "article", ["t1"])

    # near-doc has TWO chunks: one IDENTICAL to the query embedding
    # (similarity 1.0) and one orthogonal to it (similarity 0.0) — search
    # must return only the nearer one for that content_id.
    await _insert_chunk(db_pool, near_id, 0, "near-exact-chunk", qvec)
    await _insert_chunk(db_pool, near_id, 1, "near-doc-other-chunk", _unit_at(600))
    await _insert_chunk(db_pool, mid_id, 0, "mid-chunk", _unit_at(1))  # cosine sim ~0.447
    await _insert_chunk(db_pool, far_id, 0, "far-chunk", _unit_at(500))  # orthogonal, sim 0.0

    # Unfiltered: all 3 docs, one row each, ordered by similarity desc.
    hits = await store.search(query, limit=10)
    hits = [h for h in hits if h["content_id"] in (near_id, mid_id, far_id)]
    assert [h["content_id"] for h in hits] == [near_id, mid_id, far_id]
    sims = [h["similarity"] for h in hits]
    assert sims == sorted(sims, reverse=True)

    near_hit = hits[0]
    assert near_hit["content"] == "near-exact-chunk"  # nearest chunk wins, not the other one
    assert near_hit["similarity"] == pytest.approx(1.0, abs=1e-6)
    assert hits[1]["similarity"] == pytest.approx(0.4472136, abs=1e-6)
    assert hits[2]["similarity"] == pytest.approx(0.0, abs=1e-6)

    # source_type filter narrows to the matching doc only.
    report_hits = await store.search(query, limit=10, source_type="reportX")
    assert [h["content_id"] for h in report_hits] == [mid_id]

    # tags filter (array overlap) narrows to t1-tagged docs only.
    t1_hits = await store.search(query, limit=10, tags=["t1"])
    assert {h["content_id"] for h in t1_hits} == {near_id, far_id}

    # content_id-scoped search (the special-cased filtered-first path) still
    # dedupes to the single nearest chunk of that one document.
    scoped = await store.search(query, limit=5, content_id=near_id)
    assert len(scoped) == 1
    assert scoped[0]["content"] == "near-exact-chunk"
