"""People registry service — CRUD over `life.people` (migration 016).

`life.people` is the user-curated list of the humans in their life: name,
the other names/emails they go by, the relationship, key dates (birthday,
anniversary), free-form notes, and when they were last in contact.

Shaped after `services/infra.py`: plain dicts in and out over an asyncpg
pool, no ORM, so the admin CRUD routes and (later) worker activities and
chat tools can all call the same functions.

ponytail: this is a lookup table with a search function, not a CRM. No
merge/dedup engine, no fuzzy name matching — two rows for the same human is
a user problem to fix in the UI, not a heuristic to guess at.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
import structlog

logger = structlog.get_logger()

_SELECT_COLS = (
    "id, name, aliases, relationship, key_dates, notes, last_contact, "
    "metadata, created_at, updated_at"
)

# Fields an operator (or a later enrichment pass) may set through
# create/update. `id`, `created_at` and `updated_at` are DB-owned.
_EDITABLE_FIELDS = (
    "name",
    "aliases",
    "relationship",
    "key_dates",
    "notes",
    "last_contact",
    "metadata",
)


def normalize_aliases(values: Any) -> list[str]:
    """Lowercase, strip, drop blanks, de-duplicate (order preserved).

    Aliases are stored lowercased so lookup is case-insensitive through the
    GIN containment index (`aliases @> ARRAY[$1]`) rather than a sequential
    scan over `lower(unnest(aliases))`. Every write path must go through
    here or `find_people` will silently miss mixed-case aliases.
    """
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    for value in values:
        alias = str(value).strip().lower()
        if alias and alias not in out:
            out.append(alias)
    return out


async def list_people(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch(f"SELECT {_SELECT_COLS} FROM life.people ORDER BY name")
    return [dict(r) for r in rows]


async def find_people(pool: asyncpg.Pool, query: str) -> list[dict]:
    """People matching `query` on name OR alias, case-insensitively.

    Exact match on either side (no substring/fuzzy matching) — a person is
    looked up by a name someone actually used, and both branches ride an
    index. Returns [] for a blank query rather than the whole table.
    """
    needle = (query or "").strip().lower()
    if not needle:
        return []
    rows = await pool.fetch(
        f"SELECT {_SELECT_COLS} FROM life.people "
        "WHERE lower(name) = $1 OR aliases @> ARRAY[$1]::text[] ORDER BY name",
        needle,
    )
    return [dict(r) for r in rows]


async def get_person(pool: asyncpg.Pool, person_id: UUID | str) -> dict | None:
    row = await pool.fetchrow(f"SELECT {_SELECT_COLS} FROM life.people WHERE id = $1", person_id)
    return dict(row) if row else None


async def create_person(pool: asyncpg.Pool, data: dict[str, Any]) -> dict:
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    row = await pool.fetchrow(
        "INSERT INTO life.people "
        "(name, aliases, relationship, key_dates, notes, last_contact, metadata) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7) "
        f"RETURNING {_SELECT_COLS}",
        name,
        normalize_aliases(data.get("aliases")),
        data.get("relationship"),
        data.get("key_dates") or {},
        data.get("notes"),
        data.get("last_contact"),
        data.get("metadata") or {},
    )
    return dict(row)


async def update_person(
    pool: asyncpg.Pool, person_id: UUID | str, data: dict[str, Any]
) -> dict | None:
    """Patch the editable fields present in `data`. Returns None if no such row.

    `None` values are treated as "not supplied" (infra.py convention) — clear
    a text field with "" rather than null.
    """
    fields = {k: v for k, v in data.items() if k in _EDITABLE_FIELDS and v is not None}
    if "name" in fields:
        name = str(fields["name"]).strip()
        if not name:
            raise ValueError("name cannot be blank")
        fields["name"] = name
    if "aliases" in fields:
        fields["aliases"] = normalize_aliases(fields["aliases"])
    if not fields:
        return await get_person(pool, person_id)

    set_clauses = []
    values: list[Any] = [person_id]
    for i, (key, value) in enumerate(fields.items(), start=2):
        set_clauses.append(f"{key} = ${i}")
        values.append(value)
    set_sql = ", ".join(set_clauses)

    row = await pool.fetchrow(
        f"UPDATE life.people SET {set_sql}, updated_at = now() "
        f"WHERE id = $1 RETURNING {_SELECT_COLS}",
        *values,
    )
    return dict(row) if row else None


async def delete_person(pool: asyncpg.Pool, person_id: UUID | str) -> bool:
    """Delete by id. Returns False (no exception) when the row didn't exist."""
    result = await pool.execute("DELETE FROM life.people WHERE id = $1", person_id)
    return result != "DELETE 0"
