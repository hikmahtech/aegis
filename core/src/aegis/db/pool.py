"""Database pool and migration runner for AEGIS v2."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import asyncpg
import structlog

logger = structlog.get_logger()


def _encode_jsonb(value: Any) -> str:
    """Encode a Python object as jsonb, rejecting already-serialized dict/list strings.

    jsonb columns legitimately store bare Python strings as JSON string scalars
    (e.g. `settings.value = "UTC"`, `social_publish_label = "publish"` — the
    generic `settings` key/value store and several call sites depend on this).
    That is NOT the bug.

    The actual recurring bug (issue #37 / PR #79): a caller pre-serializes a
    dict/list with `json.dumps` and passes the resulting string here, which
    this codec's encoder then encodes *again*, landing as a jsonb string
    scalar containing escaped JSON text instead of a jsonb object/array
    (`col->>'key'` then returns NULL). Detect that specific mistake — a string
    that itself parses as a JSON object or array is almost certainly a
    pre-dumped payload, not an intentional scalar value — and fail loudly at
    the call site instead of silently corrupting data. bytes/bytearray have no
    legitimate jsonb use here, so those are always rejected.
    """
    if isinstance(value, (bytes, bytearray)):
        raise TypeError(
            "jsonb parameters must be Python objects (dict/list/str/...), not bytes — "
            "the pool codec applies json.dumps itself"
        )
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            raise TypeError(
                "jsonb parameter looks pre-dumped (a JSON object/array encoded as a "
                "string) — the pool codec applies json.dumps; pass the dict/list "
                "directly instead of a json.dumps(...) string, which would double-"
                "encode it into a jsonb string scalar"
            )
    return json.dumps(value)


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Set up the JSONB codec and pgvector's filtered-search behaviour."""
    await conn.set_type_codec(
        "jsonb",
        encoder=_encode_jsonb,
        decoder=json.loads,
        schema="pg_catalog",
    )
    # pgvector's HNSW scan stops after `hnsw.ef_search` candidates (default 40)
    # NO MATTER what LIMIT the query asks for. `KnowledgeStore.search` is a
    # two-stage "ANN candidates, then filter" query, so that cap lands upstream
    # of every source_type/tags filter: measured in prod, an inner LIMIT of 400,
    # 2000 and 10000 each returned exactly 40 rows, and a search filtered to
    # source_type='intelligence' (0.05% of the corpus) returned 1 document.
    # `iterative_scan` keeps scanning until the LIMIT is satisfied instead, so
    # the existing oversample actually means something. It is also FASTER here
    # (16ms vs 28ms measured) because it stops as soon as it has enough.
    # `relaxed_order` rather than `strict_order`: the outer query already
    # re-sorts by similarity, so we don't pay for ordering pgvector would only
    # have to redo. Requires pgvector >= 0.8; older builds don't know the GUC,
    # so tolerate that rather than making the whole pool un-creatable.
    try:
        await conn.execute("SET hnsw.iterative_scan = relaxed_order")
    except asyncpg.PostgresError as exc:
        logger.warning("hnsw_iterative_scan_unavailable", error=str(exc)[:200])


async def create_pool(database_url: str, min_size: int = 2, max_size: int = 10) -> asyncpg.Pool:
    """Create and return an asyncpg connection pool."""
    pool = await asyncpg.create_pool(
        database_url, min_size=min_size, max_size=max_size, init=_init_connection
    )
    logger.info("db_pool_created", min_size=min_size, max_size=max_size)
    return pool


async def run_migrations(pool: asyncpg.Pool, migrations_dir: str | Path = "migrations") -> None:
    """Run pending SQL migrations, tracked by schema_migrations table.

    Uses advisory lock to prevent concurrent runs. Fails fast on error.
    """
    migrations_path = Path(migrations_dir)
    if not migrations_path.exists():
        logger.warning("migrations_dir_not_found", path=str(migrations_path))
        return

    sql_files = sorted(migrations_path.glob("*.sql"))
    if not sql_files:
        logger.info("no_migrations_found")
        return

    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock(hashtext('aegis_migrations'))")
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    filename TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            applied = {
                row["filename"]
                for row in await conn.fetch("SELECT filename FROM schema_migrations")
            }
            pending = [f for f in sql_files if f.name not in applied]
            if not pending:
                logger.info("migrations_up_to_date", total=len(sql_files))
                return

            for sql_file in pending:
                sql = sql_file.read_text()
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES ($1)", sql_file.name
                )
                logger.info("migration_applied", file=sql_file.name)

            logger.info("migrations_complete", applied=len(pending), total=len(sql_files))
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtext('aegis_migrations'))")


async def check_health(pool: asyncpg.Pool) -> dict[str, Any]:
    """Check database connectivity."""
    t0 = time.monotonic()
    try:
        result = await pool.fetchval("SELECT 1")
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        return {"status": "ok" if result == 1 else "error", "latency_ms": latency_ms}
    except Exception as e:
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        logger.warning("db_health_check_failed", error=str(e))
        return {"status": "error", "latency_ms": latency_ms}
