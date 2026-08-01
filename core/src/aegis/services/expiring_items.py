"""Expiry-radar registry service — CRUD over `life.expiring_items` (migration 018).

Everything with a renewal/expiry date: passport, visa, licence, insurance
policy, warranty, medication refill, domain. The daily radar flow (C6) reads
this through `due_within`; the admin panel writes it through the CRUD
functions.

Shaped after `services/people.py`: plain dicts in and out over an asyncpg
pool, no ORM, so admin routes, worker activities and chat tools can all call
the same functions.

ponytail: no recurrence engine. Renewal is "edit expires_on to the new date",
which also re-arms every alert threshold for free (the dedup key in
`life.expiring_item_alerts` includes expires_on). A yearly-recurring item does
not need a cron expression — it needs the user to type the new date once a
year, which they must do anyway because the real date is on the document.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
import structlog

logger = structlog.get_logger()

_SELECT_COLS = (
    "id, kind, title, expires_on, lead_days, person_id, asset_id, notes, "
    "metadata, created_at, updated_at"
)

# Fields an operator may set through create/update. `id`, `created_at` and
# `updated_at` are DB-owned.
_EDITABLE_FIELDS = (
    "kind",
    "title",
    "expires_on",
    "lead_days",
    "person_id",
    "asset_id",
    "notes",
    "metadata",
)

# Used when a caller supplies no lead_days at all. Mirrors the column DEFAULT
# in migration 018 — kept here too so `create_expiring_item` always writes an
# explicit, normalised array rather than relying on the DB default for some
# paths and the service for others.
DEFAULT_LEAD_DAYS = (30, 7, 1)


def normalize_lead_days(values: Any) -> list[int]:
    """De-duplicate, drop negatives/junk, sort descending.

    Descending because that is the order the thresholds are crossed in, so a
    consumer walking the list hits the widest warning first. Negative leads
    are dropped (a warning "10 days after it expired" is what `due_within`
    already surfaces as past-due); 0 is kept and means "on the day".

    Every write path must go through here — an unsorted or duplicated array
    would make C6 fire the same threshold twice per cycle.
    """
    if values is None:
        return list(DEFAULT_LEAD_DAYS)
    if isinstance(values, int):
        values = [values]
    out: set[int] = set()
    for value in values:
        try:
            day = int(value)
        except (TypeError, ValueError):
            continue
        if day >= 0:
            out.add(day)
    if not out:
        return list(DEFAULT_LEAD_DAYS)
    return sorted(out, reverse=True)


async def list_expiring_items(pool: asyncpg.Pool, kind: str | None = None) -> list[dict]:
    """All items, soonest expiry first. Optionally filtered to one `kind`."""
    if kind:
        rows = await pool.fetch(
            f"SELECT {_SELECT_COLS} FROM life.expiring_items "
            "WHERE kind = $1 ORDER BY expires_on, title",
            kind.strip().lower(),
        )
    else:
        rows = await pool.fetch(
            f"SELECT {_SELECT_COLS} FROM life.expiring_items ORDER BY expires_on, title"
        )
    return [dict(r) for r in rows]


async def due_within(pool: asyncpg.Pool, days: int) -> list[dict]:
    """Items expiring within `days` days, INCLUDING already-expired ones.

    Past-due items are included on purpose: an expired passport is the most
    urgent row in the table, not the least, and dropping it would make the
    radar go quiet at exactly the wrong moment. They come back with a
    negative `days_left`.

    `days_left` is computed here (`expires_on - CURRENT_DATE`) so C6 does not
    have to redo date arithmetic inside a Temporal workflow, where `date.today()`
    is non-deterministic and banned.
    """
    rows = await pool.fetch(
        f"SELECT {_SELECT_COLS}, (expires_on - CURRENT_DATE) AS days_left "
        "FROM life.expiring_items "
        "WHERE expires_on <= CURRENT_DATE + $1::integer "
        "ORDER BY expires_on, title",
        int(days),
    )
    return [dict(r) for r in rows]


async def get_expiring_item(pool: asyncpg.Pool, item_id: UUID | str) -> dict | None:
    row = await pool.fetchrow(
        f"SELECT {_SELECT_COLS} FROM life.expiring_items WHERE id = $1", item_id
    )
    return dict(row) if row else None


async def create_expiring_item(pool: asyncpg.Pool, data: dict[str, Any]) -> dict:
    kind = (data.get("kind") or "").strip().lower()
    if not kind:
        raise ValueError("kind is required")
    title = (data.get("title") or "").strip()
    if not title:
        raise ValueError("title is required")
    if not data.get("expires_on"):
        raise ValueError("expires_on is required")
    row = await pool.fetchrow(
        "INSERT INTO life.expiring_items "
        "(kind, title, expires_on, lead_days, person_id, asset_id, notes, metadata) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) "
        f"RETURNING {_SELECT_COLS}",
        kind,
        title,
        data.get("expires_on"),
        normalize_lead_days(data.get("lead_days")),
        data.get("person_id"),
        data.get("asset_id"),
        data.get("notes"),
        data.get("metadata") or {},
    )
    return dict(row)


async def update_expiring_item(
    pool: asyncpg.Pool, item_id: UUID | str, data: dict[str, Any]
) -> dict | None:
    """Patch the editable fields present in `data`. Returns None if no such row.

    `None` values are treated as "not supplied" (people.py / infra.py
    convention) — clear a text field with "" rather than null.
    """
    fields = {k: v for k, v in data.items() if k in _EDITABLE_FIELDS and v is not None}
    if "kind" in fields:
        kind = str(fields["kind"]).strip().lower()
        if not kind:
            raise ValueError("kind cannot be blank")
        fields["kind"] = kind
    if "title" in fields:
        title = str(fields["title"]).strip()
        if not title:
            raise ValueError("title cannot be blank")
        fields["title"] = title
    if "lead_days" in fields:
        fields["lead_days"] = normalize_lead_days(fields["lead_days"])
    if not fields:
        return await get_expiring_item(pool, item_id)

    set_clauses = []
    values: list[Any] = [item_id]
    for i, (key, value) in enumerate(fields.items(), start=2):
        set_clauses.append(f"{key} = ${i}")
        values.append(value)
    set_sql = ", ".join(set_clauses)

    row = await pool.fetchrow(
        f"UPDATE life.expiring_items SET {set_sql}, updated_at = now() "
        f"WHERE id = $1 RETURNING {_SELECT_COLS}",
        *values,
    )
    return dict(row) if row else None


async def delete_expiring_item(pool: asyncpg.Pool, item_id: UUID | str) -> bool:
    """Delete by id. Returns False (no exception) when the row didn't exist.

    The item's rows in `life.expiring_item_alerts` go with it (ON DELETE
    CASCADE) — the dedup ledger is meaningless once the item is gone.
    """
    result = await pool.execute("DELETE FROM life.expiring_items WHERE id = $1", item_id)
    return result != "DELETE 0"
