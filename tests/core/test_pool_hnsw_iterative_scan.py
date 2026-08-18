"""Every pooled connection must disable pgvector's candidate cap.

`KnowledgeStore.search` is a two-stage "ANN candidates, then filter" query: an
inner `ORDER BY embedding <=> $1 LIMIT <oversample>` feeds an outer query that
applies the source_type/tags filters. pgvector's HNSW scan stops after
`hnsw.ef_search` rows (default 40) NO MATTER what that LIMIT says, so the cap
lands upstream of every filter and the oversample is decorative.

Measured in prod before this fix: an inner LIMIT of 400, 2000 and 10000 each
returned exactly 40 candidates, and a search filtered to
source_type='intelligence' (0.05% of the corpus) returned ONE document — which
is why the daily briefing's intelligence section was empty every day.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_pooled_connections_enable_iterative_scan(db_pool):
    """The setting must be live on a connection the app actually uses, not
    merely present in the source — `_init_connection` is the only thing
    standing between every filtered search and the 40-row cap."""
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SHOW hnsw.iterative_scan") == "relaxed_order"


async def test_every_connection_in_the_pool_gets_it(db_pool):
    """A pool that grew past its initial size must not hand out unconfigured
    connections."""
    async with db_pool.acquire() as a, db_pool.acquire() as b:
        assert await a.fetchval("SHOW hnsw.iterative_scan") == "relaxed_order"
        assert await b.fetchval("SHOW hnsw.iterative_scan") == "relaxed_order"


async def test_it_survives_a_connection_going_back_to_the_pool(db_pool):
    """The regression that shipped: asyncpg runs `RESET ALL` on release, so a
    session `SET` from `init=` applies on a connection's FIRST use and is gone
    on every reuse after that. Both tests above pass either way, because both
    take freshly-initialised connections — this is the one that tells them
    apart. Found in prod: the second acquire reported an empty value and the
    ANN scan fell back to 40 candidates.
    """
    seen = []
    for _ in range(3):
        # Sequential acquires, each releasing before the next, so the pool
        # hands the SAME connection back after a reset.
        async with db_pool.acquire() as conn:
            seen.append(await conn.fetchval("SHOW hnsw.iterative_scan"))
    assert seen == ["relaxed_order"] * 3, f"lost after release/reacquire: {seen}"


async def test_an_ann_scan_is_not_capped_at_ef_search(db_pool):
    """The behaviour itself, on real rows: ask the index for far more
    candidates than `ef_search` and confirm the scan actually returns them.

    Seeds its own chunks rather than skipping on an empty database — a test
    that skips in CI guards nothing, and this is the assertion the whole fix
    exists for. `enable_seqscan = off` is what makes it meaningful: with a
    small table Postgres would otherwise seq-scan, which honours the LIMIT
    natively and would pass whether or not the GUC is set.

    Uses a raw ANN query rather than `KnowledgeStore.search` so no embedding
    model is needed — the cap is a property of the index scan, not of what the
    query vector means.
    """
    n_rows, dims, want = 300, 768, 100
    count_sql = f"""
        SELECT count(*) FROM (
            SELECT k.content_id FROM knowledge_chunks k
            ORDER BY k.embedding <=> $1::vector
            LIMIT {want}
        ) cand
    """
    try:
        await db_pool.execute(
            "INSERT INTO knowledge_content (content_id, title, source_type) "
            "SELECT 'efprobe'||i, 'p'||i, 'efsearch_probe' "
            "FROM generate_series(1, $1) i ON CONFLICT DO NOTHING",
            n_rows,
        )
        await db_pool.execute(
            "INSERT INTO knowledge_chunks (content_id, chunk_index, chunk_text, embedding) "
            "SELECT 'efprobe'||i, 0, 'probe', "
            "  (SELECT ('['||string_agg(CASE WHEN d = mod(i, $2) THEN '1' ELSE '0' END, ',')||']')"
            "   ::vector FROM generate_series(0, $2 - 1) d) "
            "FROM generate_series(1, $1) i",
            n_rows,
            dims,
        )
        probe = "[" + ",".join("1" if j == 1 else "0" for j in range(dims)) + "]"

        # Compare the SAME query under both settings on the SAME data, so the
        # assertion can't be satisfied by a lucky row distribution or a planner
        # choice. `enable_seqscan = off` keeps both on the index — a seq scan
        # honours LIMIT natively and would hide the difference.
        #
        # Two SEPARATE connections, deliberately: asyncpg caches a prepared
        # statement per connection, so running both measurements on one
        # connection reuses the first plan and both come back capped — which
        # reads as "the fix doesn't work" when it does.
        # Both held open at once so the pool cannot hand the same connection
        # back for the second measurement.
        async with db_pool.acquire() as capped_conn, db_pool.acquire() as pooled_conn:
            async with capped_conn.transaction():
                await capped_conn.execute("SET LOCAL enable_seqscan = off")
                await capped_conn.execute("SET LOCAL hnsw.iterative_scan = off")
                capped = await capped_conn.fetchval(count_sql, probe)
            async with pooled_conn.transaction():
                await pooled_conn.execute("SET LOCAL enable_seqscan = off")
                # no iterative_scan SET here: this is what `_init_connection` left
                pooled = await pooled_conn.fetchval(count_sql, probe)

        assert capped < want, (
            f"expected the ef_search cap to bite with iterative_scan off, got {capped}/{want} — "
            "the probe no longer reproduces the bug, so it cannot prove the fix"
        )
        # Deliberately a margin, not `== want`: `relaxed_order` is approximate
        # recall — it keeps scanning until it has enough or the graph is
        # exhausted, so the exact count varies with the index's shape. What
        # must hold is that it is no longer pinned near `ef_search`.
        assert pooled >= 2 * capped, (
            f"pooled connection returned {pooled} candidates against the capped {capped} "
            "— _init_connection's hnsw.iterative_scan is not taking effect"
        )
    finally:
        await db_pool.execute(
            "DELETE FROM knowledge_content WHERE source_type = 'efsearch_probe'"
        )
