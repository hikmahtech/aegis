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
    """Set up JSONB codec so asyncpg returns Python objects, not strings."""
    await conn.set_type_codec(
        "jsonb",
        encoder=_encode_jsonb,
        decoder=json.loads,
        schema="pg_catalog",
    )


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
