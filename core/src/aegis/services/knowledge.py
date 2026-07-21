"""Native pgvector knowledge subsystem.

Replaces the external knowledge-service. Stores ingested content + embedded
chunks in the app's own Postgres (pgvector) and serves semantic search and a
small RAG `ask()` helper over the local LLM. No external service, no HTTP hop.

`KnowledgeStore` keeps the same method names / signatures / return shapes the
old `KnowledgeConnector` exposed, so existing call sites (chat tools, ingest
activities, admin/reference routes) need no change — they just hold a
`KnowledgeStore` instead of a connector.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    import asyncpg

    from aegis.llm import LLMClient

logger = structlog.get_logger()

_CHUNK_SIZE = 1500
_CHUNK_OVERLAP = 200


def _content_id_for(url: str) -> str:
    """Stable content id from the source URL — re-ingesting a URL upserts."""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()  # noqa: S324  (id, not security)


def _strip_nul(text: str | None) -> str | None:
    """Drop NUL bytes Postgres' UTF8 encoding rejects outright.

    PDF extraction (pdfminer) and other extractors occasionally emit `\\x00`
    from malformed source bytes. Postgres raises
    `invalid byte sequence for encoding "UTF8": 0x00` on any text column
    write containing one — asyncpg surfaces this as a DataError on the
    INSERT. Applied once here (the common choke point every ingestion
    source funnels through) so PDFs, HTML, uploads, transcripts, etc. are
    all covered without touching each extractor.
    """
    return text.replace("\x00", "") if text else text


def _chunk(text: str, size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """Naive fixed-size char chunking with overlap.

    ponytail: a char splitter is enough at personal scale; swap for semantic
    chunking only if eval shows retrieval needs it.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    step = max(1, size - overlap)
    while start < len(text):
        chunks.append(text[start : start + size])
        start += step
    return chunks


def _vec_literal(embedding: list[float]) -> str:
    """Render a float vector as a pgvector text literal for `$n::vector`.

    ponytail: text-literal cast avoids a pgvector-python dependency + per-conn
    type registration; switch to register_vector only if this shows up in a
    profile.
    """
    return "[" + ",".join(f"{x:.7g}" for x in embedding) + "]"


class KnowledgeStore:
    """pgvector-backed knowledge store + RAG. Drop-in for the old connector."""

    def __init__(self, db_pool: asyncpg.Pool, llm: LLMClient, embedding_model: str):
        self._pool = db_pool
        self._llm = llm
        self._embedding_model = embedding_model

    async def _embed_one(self, text: str) -> list[float]:
        vecs = await self._llm.embed([text], model=self._embedding_model)
        return vecs[0] if vecs else []

    # --- Write ---

    async def ingest_content(
        self,
        url: str,
        title: str,
        source_type: str,
        summary: str | None = None,
        raw_text: str | None = None,
        tags: list[str] | None = None,
        knowledge: list[dict] | None = None,  # noqa: ARG002  (accepted for back-compat, unused)
        metadata: dict | None = None,
        domains: list[str] | None = None,  # noqa: ARG002  (accepted for back-compat, unused)
    ) -> dict:
        """Ingest a document: upsert content row, (re)embed its chunks.

        Returns {content_id, status, chunks_total}. status is "ok" on success,
        "empty" when there's no embeddable text.
        """
        content_id = _content_id_for(url)
        title = _strip_nul(title) or ""
        summary = _strip_nul(summary)
        raw_text = _strip_nul(raw_text)
        body = raw_text or summary or title or ""
        chunks = _chunk(body)
        if not chunks:
            return {"content_id": content_id, "status": "empty", "chunks_total": 0}

        embeddings = await self._llm.embed(chunks, model=self._embedding_model)
        if len(embeddings) != len(chunks):
            raise ValueError(
                f"embed returned {len(embeddings)} vectors for {len(chunks)} chunks"
            )

        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO knowledge_content
                    (content_id, url, title, source_type, summary, tags, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (content_id) DO UPDATE SET
                    url = EXCLUDED.url, title = EXCLUDED.title,
                    source_type = EXCLUDED.source_type, summary = EXCLUDED.summary,
                    tags = EXCLUDED.tags, metadata = EXCLUDED.metadata,
                    ingested_at = now()
                """,
                content_id, url, title, source_type, summary,
                list(tags or []), metadata or {},
            )
            # Replace chunks (re-ingest of the same URL refreshes the body).
            await conn.execute("DELETE FROM knowledge_chunks WHERE content_id = $1", content_id)
            for i, (chunk_text, emb) in enumerate(zip(chunks, embeddings, strict=True)):
                await conn.execute(
                    """
                    INSERT INTO knowledge_chunks (content_id, chunk_index, chunk_text, embedding)
                    VALUES ($1, $2, $3, $4::vector)
                    """,
                    content_id, i, chunk_text, _vec_literal(emb),
                )

        logger.info(
            "knowledge_content_ingested",
            content_id=content_id, source_type=source_type, chunks_total=len(chunks),
        )
        return {"content_id": content_id, "status": "ok", "chunks_total": len(chunks)}

    # --- Read ---

    async def search(
        self,
        query: str,
        limit: int = 10,
        source_type: str | None = None,
        tags: list[str] | None = None,
        content_id: str | None = None,
    ) -> list[dict]:
        """Semantic search over ingested content. One best chunk per document.

        Two-stage "ANN candidates, then filter" (the standard pgvector
        pattern): an inner subquery orders by raw vector distance and takes
        the closest `oversample` chunks — that ORDER BY ... LIMIT is what
        lets the planner use the `knowledge_chunks_embedding_idx` HNSW index
        instead of a full-table scan. The outer query then joins content,
        applies source_type/tags/content_id filters, and DISTINCT ON
        (content_id) keeps the nearest chunk per document, ordered by
        similarity. A single-stage `ORDER BY content_id, distance` (the old
        shape) can't use the vector index at all — Postgres has to compute
        the distance for every chunk in the table before it can sort/dedupe,
        which degraded to a full scan of all 193k+ chunks in prod and blew
        past the activity timeout (issue: IntelligenceScanFlow dedup_items).
        """
        qvec = await self._embed_one(query)
        if not qvec:
            return []
        # ponytail: oversampling (limit * 20, floor 200) is a fixed guess at
        # how many ANN candidates survive the source_type/tags/content_id
        # filters. A very selective filter can still legitimately return
        # fewer than `limit` matches even though more exist further out in
        # the corpus. If that ever matters in practice, upgrade to iterative
        # widening (re-run with 2x/4x oversample when the filtered result is
        # short) instead of raising the fixed multiplier for everyone.
        oversample = max(limit * 20, 200)
        rows = await self._pool.fetch(
            """
            SELECT * FROM (
                SELECT DISTINCT ON (c.content_id)
                    c.content_id, c.title, c.url, c.source_type, c.tags,
                    c.metadata, c.summary, c.ingested_at,
                    cand.chunk_text AS content,
                    1 - cand.dist AS similarity
                FROM (
                    -- content_id (single-doc search) is filtered here, inside
                    -- the ANN stage, not left to the outer WHERE: an equality
                    -- filter on one document is highly selective, so Postgres
                    -- plans it as a cheap content_id-index bitmap scan over
                    -- that doc's handful of chunks (confirmed via EXPLAIN),
                    -- skipping the vector index entirely rather than fighting
                    -- it — correct either way since DISTINCT ON downstream
                    -- collapses to the single matching content_id regardless.
                    SELECT k.content_id, k.chunk_text,
                           k.embedding <=> $1::vector AS dist
                    FROM knowledge_chunks k
                    WHERE ($3::text IS NULL OR k.content_id = $3)
                    ORDER BY k.embedding <=> $1::vector
                    LIMIT $6
                ) cand
                JOIN knowledge_content c ON c.content_id = cand.content_id
                WHERE ($2::text IS NULL OR c.source_type = $2)
                  AND ($4::text[] IS NULL OR c.tags && $4::text[])
                ORDER BY c.content_id, cand.dist
            ) s
            ORDER BY s.similarity DESC
            LIMIT $5
            """,
            _vec_literal(qvec), source_type, content_id,
            list(tags) if tags else None, limit, oversample,
        )
        return [self._row_to_result(r) for r in rows]

    @staticmethod
    def _row_to_result(r: Any) -> dict:
        d = dict(r)
        d["id"] = d["content_id"]
        d["created_at"] = d.get("ingested_at")
        d["tags"] = list(d.get("tags") or [])
        d["metadata"] = d.get("metadata") or {}
        ts = d.get("ingested_at")
        if ts is not None and not isinstance(ts, str):
            d["ingested_at"] = ts.isoformat()
            d["created_at"] = d["ingested_at"]
        return d

    async def ask(
        self,
        question: str,
        max_sources: int = 5,
        min_confidence: float = 0.0,
    ) -> dict:
        """RAG: retrieve top sources, synthesize an answer with the local LLM."""
        sources = await self.search(question, limit=max_sources)
        sources = [s for s in sources if s.get("similarity", 0) >= min_confidence]
        if not sources:
            return {"answer": "", "sources": [], "confidence": 0.0}
        context = "\n\n".join(
            f"[{i + 1}] {s.get('title') or ''}\n{(s.get('content') or s.get('summary') or '')[:1200]}"
            for i, s in enumerate(sources)
        )
        prompt = (
            f"Answer the question using ONLY the sources below. Cite as [n]. "
            f"If the sources don't answer it, say so.\n\n"
            f"SOURCES:\n{context}\n\nQUESTION: {question}"
        )
        result = await self._llm.think(prompt, max_tokens=1000, db_pool=self._pool, purpose="knowledge_ask")
        return {
            "answer": result.get("response", ""),
            "sources": [
                {"title": s.get("title"), "url": s.get("url"), "similarity": s.get("similarity")}
                for s in sources
            ],
            "confidence": max((s.get("similarity", 0.0) for s in sources), default=0.0),
        }

    async def list_content_items(
        self, limit: int = 200, source_type: str | None = None
    ) -> list[dict]:
        """Recent ingested content metadata, newest first."""
        rows = await self._pool.fetch(
            """
            SELECT c.content_id, c.title, c.url, c.source_type, c.tags,
                   c.metadata, c.summary, c.ingested_at,
                   (SELECT count(*) FROM knowledge_chunks k WHERE k.content_id = c.content_id)
                       AS chunks_total
            FROM knowledge_content c
            WHERE ($2::text IS NULL OR c.source_type = $2)
            ORDER BY c.ingested_at DESC
            LIMIT $1
            """,
            limit, source_type,
        )
        return [self._row_to_result(r) for r in rows]

    async def get_content_status(self, content_id: str) -> dict:
        """Per-content status. Synchronous ingest => 'completed' or 'not_found'."""
        row = await self._pool.fetchrow(
            """
            SELECT c.content_id, c.title, c.url, c.source_type, c.tags,
                   c.metadata, c.ingested_at,
                   (SELECT count(*) FROM knowledge_chunks k WHERE k.content_id = c.content_id)
                       AS chunks_total
            FROM knowledge_content c WHERE c.content_id = $1
            """,
            content_id,
        )
        # ponytail: native store has no async job queue, so 'not_found' as a
        # status dict (not a 404 raise) — callers render it fine.
        if row is None:
            return {"content_id": content_id, "status": "not_found"}
        d = self._row_to_result(row)
        d["status"] = "completed"
        return d

    async def get_content_chunks(self, content_id: str) -> list[dict]:
        """Ordered chunks of one content item (no embeddings in the payload)."""
        rows = await self._pool.fetch(
            """
            SELECT content_id, chunk_index, chunk_text
            FROM knowledge_chunks WHERE content_id = $1 ORDER BY chunk_index
            """,
            content_id,
        )
        return [dict(r) for r in rows]

    async def get_recent_jobs(self, limit: int = 20) -> list[dict]:
        """Recent ingests as job-shaped rows (no real job queue here)."""
        items = await self.list_content_items(limit=limit)
        return [
            {
                "content_id": it["content_id"],
                "title": it.get("title"),
                "status": "completed",
                "ingested_at": it.get("ingested_at"),
            }
            for it in items
        ]

    async def get_stats(self) -> dict:
        """Counts for the admin health panel."""
        async with self._pool.acquire() as conn:
            content = await conn.fetchval("SELECT count(*) FROM knowledge_content") or 0
            chunks = await conn.fetchval("SELECT count(*) FROM knowledge_chunks") or 0
        return {"content_items": content, "chunks": chunks}

    async def health(self) -> dict:
        await self._pool.fetchval("SELECT 1")
        return {"status": "ok", "backend": "pgvector"}

    async def close(self) -> None:
        """No-op — the store borrows the shared pool/LLM; it owns neither."""
        return None
