"""Shared channel-row helpers for ingest flows.

Reads and updates the channels table (kind, identifier, config jsonb).
One activity class used by every ingest flow that tracks a cursor
in channels.config.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from temporalio import activity


def _decode_config(raw) -> dict:
    """Normalise asyncpg JSONB result to a plain dict.

    When the JSONB codec is registered (create_pool sets it via set_type_codec),
    asyncpg returns a Python dict.  If the codec is absent — or if the row was
    inserted as a pre-serialised JSON string and the codec double-encoded it —
    the result can be a plain string.  Handling both prevents
    'dictionary update sequence element' errors at runtime.
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    # str (codec absent or double-encoded path)
    return json.loads(raw)


@dataclass
class ChannelRow:
    id: str
    kind: str
    identifier: str
    config: dict
    active: bool


@dataclass
class ChannelActivities:
    db_pool: Any

    @activity.defn
    async def list_active_channels(self, kind: str) -> list[dict]:
        if not self.db_pool:
            return []
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id::text as id, kind, identifier, config, active "
                "FROM channels WHERE kind=$1 AND active=true "
                "ORDER BY identifier",
                kind,
            )
        return [
            {
                "id": r["id"],
                "kind": r["kind"],
                "identifier": r["identifier"],
                "config": _decode_config(r["config"]),
                "active": r["active"],
            }
            for r in rows
        ]

    @activity.defn
    async def update_channel_config_key(
        self, kind: str, identifier: str, key: str, value: Any
    ) -> None:
        if not self.db_pool:
            return
        # Pass value as text and cast via ::text::jsonb so the JSONB codec
        # encoder does not double-serialize the value.  json.dumps handles the
        # serialisation once; Postgres casts the text literal to jsonb.
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE channels SET config = jsonb_set(config, ARRAY[$3::text], $4::text::jsonb) "
                "WHERE kind=$1 AND identifier=$2",
                kind,
                identifier,
                key,
                json.dumps(value),
            )

    @activity.defn
    async def ingest_idempotency_claim(self, source_type: str, external_id: str) -> bool:
        if not self.db_pool:
            return True
        async with self.db_pool.acquire() as conn:
            result = await conn.fetchval(
                "INSERT INTO ingest_idempotency (source_type, external_id) "
                "VALUES ($1, $2) ON CONFLICT DO NOTHING RETURNING external_id",
                source_type,
                external_id,
            )
        return result is not None
