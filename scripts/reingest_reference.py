"""Re-trigger KS extraction for an existing content_metadata row.

Use when a row has raw_text but ``ingestion_jobs.status='failed'`` (e.g. a
``ServiceRestart`` interrupted the LLM pass and nothing retried it).

The script reads the row directly from KS's Postgres, captures the full
payload into memory, deletes the failed job + any partial chunks/triples
+ the metadata row, then POSTs the payload back to KS's ``/api/content``
so it goes through the pipeline again with the current extractor logic.

Usage::

    AEGIS_KNOWLEDGE_BASE_URL=http://localhost:8081 \\
    AEGIS_KNOWLEDGE_API_KEY=... \\
    KS_PG_DSN=postgresql://aegis:...@node-a:5433/knowledge \\
        python scripts/reingest_reference.py <content_id>

The script captures the row payload into memory BEFORE wiping. If the
POST fails the payload is dumped to ``/tmp/reingest_<content_id>.json``
so it can be replayed manually — the wipe is irreversible from KS's
side but the body lives on in that dump.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import asyncpg
import httpx


async def _register_jsonb_codec(conn: asyncpg.Connection) -> None:
    """Decode ``jsonb`` columns into Python dicts/lists.

    Without this, asyncpg returns ``jsonb`` as a raw string and
    ``dict(row["metadata"])`` blows up with ``ValueError: dictionary
    update sequence element #0 has length 1; 2 is required``. The aegis
    app registers this codec on every connection at startup — standalone
    scripts have to do it themselves.
    """
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


def _payload_from_row(row: asyncpg.Record) -> dict:
    """Build the ``/api/content`` request body from a content_metadata row.

    Coerces metadata back to a dict if asyncpg handed us a string (e.g.
    when the jsonb codec wasn't registered) — defensive belt to keep the
    wipe-then-POST flow safe.
    """
    metadata = row["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata) if metadata else {}
    elif metadata is None:
        metadata = {}
    body: dict = {
        "url": row["url"],
        "title": row["title"],
        "source_type": row["source_type"] or "reference",
        "raw_text": row["raw_text"],
        "tags": list(row["tags"] or []),
        "metadata": dict(metadata),
    }
    if row["summary"]:
        body["summary"] = row["summary"]
    return body


async def _wipe_existing(conn: asyncpg.Connection, content_id: str, url: str) -> None:
    await conn.execute("DELETE FROM ingestion_jobs WHERE content_id = $1", content_id)
    await conn.execute("DELETE FROM content WHERE content_id = $1", content_id)
    await conn.execute("DELETE FROM provenance WHERE source_url = $1", url)
    await conn.execute("DELETE FROM content_metadata WHERE id = $1", content_id)


async def _reingest(content_id: str, ks_url: str, api_key: str, dsn: str) -> int:
    async with (
        asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=2,
            init=_register_jsonb_codec,
        ) as pool,
        pool.acquire() as conn,
    ):
        row = await conn.fetchrow(
            "SELECT url, title, summary, raw_text, source_type, tags, metadata "
            "FROM content_metadata WHERE id = $1",
            content_id,
        )
        if row is None:
            print(f"content_id {content_id} not found in content_metadata", file=sys.stderr)
            return 2
        if not row["raw_text"]:
            print(
                f"content_id {content_id} has empty raw_text — nothing to re-extract "
                "(refetch the body first or use the normal capture flow)",
                file=sys.stderr,
            )
            return 3

        body = _payload_from_row(row)
        await _wipe_existing(conn, content_id, row["url"])

    # Wipe done — dump a backup of the body in case the POST fails, so the
    # data isn't lost to the void.
    backup_path = f"/tmp/reingest_{content_id}.json"
    with open(backup_path, "w") as fh:
        json.dump(body, fh)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=5.0)) as client:
            resp = await client.post(
                f"{ks_url.rstrip('/')}/api/content",
                json=body,
                headers={"X-API-Key": api_key} if api_key else {},
            )
            resp.raise_for_status()
            result = resp.json()
    except Exception as exc:
        print(
            f"POST failed: {exc}\nBody saved to {backup_path} — re-POST manually with:\n"
            f"  curl -X POST -H 'X-API-Key: ...' -H 'Content-Type: application/json' "
            f"{ks_url.rstrip('/')}/api/content -d @{backup_path}",
            file=sys.stderr,
        )
        return 4

    print(
        f"reingested url={body['url']} "
        f"new_content_id={result.get('content_id')} status={result.get('status')} "
        f"chunks_total={result.get('chunks_total')}"
    )
    os.unlink(backup_path)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("content_id", help="UUID of the content_metadata row to re-extract")
    args = parser.parse_args()

    dsn = os.environ.get("KS_PG_DSN")
    ks_url = os.environ.get("AEGIS_KNOWLEDGE_BASE_URL")
    api_key = os.environ.get("AEGIS_KNOWLEDGE_API_KEY", "")
    missing = [
        k for k, v in {"KS_PG_DSN": dsn, "AEGIS_KNOWLEDGE_BASE_URL": ks_url}.items() if not v
    ]
    if missing:
        print(f"missing required env vars: {', '.join(missing)}", file=sys.stderr)
        return 1

    return asyncio.run(_reingest(args.content_id, ks_url, api_key, dsn))


if __name__ == "__main__":
    raise SystemExit(main())
