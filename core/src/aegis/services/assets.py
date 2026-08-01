"""Household/asset registry service — CRUD over `life.assets` (migration 019).

Cars, appliances, home systems: the physical things that get bought,
warrantied and serviced. Generalises the `services/infra.py` shape (slug +
name + open `kind` + metadata) without any of infra's credentials/SSH/
provisioning machinery — this is data, not an actuation target.

Shaped after `services/expiring_items.py` and `services/people.py`: plain
dicts in and out over an asyncpg pool, no ORM, so admin routes, worker
activities and chat tools can all call the same functions.

The one place this touches another table is the **service-due mirror**: an
asset with both `service_interval_days` and `last_serviced_at` set gets a
`life.expiring_items` row (kind='asset_service') so the existing daily expiry
radar surfaces it — no new flow, no new schedule.

ponytail: no service *history* table. `last_serviced_at` is a single date,
because the question the radar asks is "is it due?", which needs only the
most recent service. A full log is a different feature with a different UI;
if it ever exists, it can be a child table then.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any
from uuid import UUID

import asyncpg

_SELECT_COLS = (
    "id, slug, name, kind, purchase_date, warranty_until, service_interval_days, "
    "last_serviced_at, location, notes, metadata, created_at, updated_at"
)

# Fields an operator may set through create/update. `id`, `created_at` and
# `updated_at` are DB-owned; `slug` is set once at create time (renaming the
# slug would orphan every `asset:<slug>`-tagged manual, so it is deliberately
# not editable).
_EDITABLE_FIELDS = (
    "name",
    "kind",
    "purchase_date",
    "warranty_until",
    "service_interval_days",
    "last_serviced_at",
    "location",
    "notes",
    "metadata",
)

# The `kind` the service-due mirror writes into life.expiring_items. Anything
# with this kind AND an asset_id is machine-generated and owned by this
# module — see `sync_service_due` / `delete_asset`.
SERVICE_KIND = "asset_service"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or "asset"


async def _unique_slug(pool: asyncpg.Pool, base: str) -> str:
    """Suffix until free. Mirrors services/infra.py.

    The column is UNIQUE, and the create route does not catch
    asyncpg.UniqueViolationError — so two assets both called "Fridge" would
    otherwise be an HTTP 500 instead of `fridge` and `fridge-2`.
    """
    slug = base
    n = 2
    while await pool.fetchval("SELECT 1 FROM life.assets WHERE slug = $1", slug):
        slug = f"{base}-{n}"
        n += 1
    return slug


# ---------------------------------------------------------------------------
# Service-due mirror into life.expiring_items (migration 018)
# ---------------------------------------------------------------------------


def service_due_on(asset: dict[str, Any]) -> date | None:
    """`last_serviced_at + service_interval_days`, or None if not applicable.

    Both fields are required: an interval with no last-service date has no
    anchor to count from, and a last-service date with no interval says
    nothing about when the next one is due. A non-positive interval is
    treated as "not applicable" rather than as a reminder due today-or-earlier.
    """
    interval = asset.get("service_interval_days")
    last = asset.get("last_serviced_at")
    if not interval or not last:
        return None
    try:
        days = int(interval)
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    return last + timedelta(days=days)


async def sync_service_due(pool: asyncpg.Pool, asset: dict[str, Any]) -> str | None:
    """Reconcile the asset's `asset_service` row in life.expiring_items.

    Returns "upserted", "cleared" or None (nothing to do). Idempotent.

    Moving `last_serviced_at` forward moves `expires_on` forward, which
    re-arms every alert threshold for free — the dedup key in
    life.expiring_item_alerts includes expires_on (migration 018).

    Clearing either field deletes the mirror row: a reminder computed from
    inputs that no longer exist would nag forever with nothing to recompute
    it from.

    No ON CONFLICT / unique index: adding a unique constraint to the
    already-shipped life.expiring_items would turn a hand-made duplicate on
    the Expiry page into an HTTP 500 there. A single-user admin CRUD path does
    not need the race protection that would cost.
    """
    asset_id = asset["id"]
    due = service_due_on(asset)
    if due is None:
        result = await pool.execute(
            "DELETE FROM life.expiring_items WHERE asset_id = $1 AND kind = $2",
            asset_id,
            SERVICE_KIND,
        )
        return "cleared" if result != "DELETE 0" else None

    title = f"Service due: {asset['name']}"
    updated = await pool.fetchval(
        "UPDATE life.expiring_items SET expires_on = $3, title = $4, updated_at = now() "
        "WHERE asset_id = $1 AND kind = $2 RETURNING id",
        asset_id,
        SERVICE_KIND,
        due,
        title,
    )
    if updated is None:
        await pool.execute(
            "INSERT INTO life.expiring_items (kind, title, expires_on, asset_id) "
            "VALUES ($1, $2, $3, $4)",
            SERVICE_KIND,
            title,
            due,
            asset_id,
        )
    return "upserted"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def list_assets(pool: asyncpg.Pool, kind: str | None = None) -> list[dict]:
    """All assets, alphabetical. Optionally filtered to one `kind`."""
    if kind:
        rows = await pool.fetch(
            f"SELECT {_SELECT_COLS} FROM life.assets WHERE kind = $1 ORDER BY name",
            kind.strip().lower(),
        )
    else:
        rows = await pool.fetch(f"SELECT {_SELECT_COLS} FROM life.assets ORDER BY name")
    return [dict(r) for r in rows]


async def get_asset(pool: asyncpg.Pool, asset_id: UUID | str) -> dict | None:
    row = await pool.fetchrow(f"SELECT {_SELECT_COLS} FROM life.assets WHERE id = $1", asset_id)
    return dict(row) if row else None


async def create_asset(pool: asyncpg.Pool, data: dict[str, Any]) -> dict:
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    kind = (data.get("kind") or "").strip().lower()
    if not kind:
        raise ValueError("kind is required")
    slug = await _unique_slug(pool, slugify(str(data.get("slug") or "").strip() or name))

    row = await pool.fetchrow(
        "INSERT INTO life.assets "
        "(slug, name, kind, purchase_date, warranty_until, service_interval_days, "
        " last_serviced_at, location, notes, metadata) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) "
        f"RETURNING {_SELECT_COLS}",
        slug,
        name,
        kind,
        data.get("purchase_date"),
        data.get("warranty_until"),
        data.get("service_interval_days"),
        data.get("last_serviced_at"),
        data.get("location"),
        data.get("notes"),
        data.get("metadata") or {},
    )
    asset = dict(row)
    await sync_service_due(pool, asset)
    return asset


async def update_asset(
    pool: asyncpg.Pool, asset_id: UUID | str, data: dict[str, Any]
) -> dict | None:
    """Patch the editable fields present in `data`. Returns None if no such row.

    `None` values are treated as "not supplied" (people.py / infra.py /
    expiring_items.py convention) — with one deliberate exception: the two
    service-mirror inputs, where `None` means "clear it", because clearing
    them is how an operator turns a service reminder OFF and there is no
    empty-string equivalent for an integer or a date.
    """
    clearable = {"service_interval_days", "last_serviced_at"}
    fields = {
        k: v
        for k, v in data.items()
        if k in _EDITABLE_FIELDS and (v is not None or k in clearable)
    }
    if "name" in fields:
        name = str(fields["name"]).strip()
        if not name:
            raise ValueError("name cannot be blank")
        fields["name"] = name
    if "kind" in fields:
        kind = str(fields["kind"]).strip().lower()
        if not kind:
            raise ValueError("kind cannot be blank")
        fields["kind"] = kind
    if not fields:
        return await get_asset(pool, asset_id)

    set_clauses = []
    values: list[Any] = [asset_id]
    for i, (key, value) in enumerate(fields.items(), start=2):
        set_clauses.append(f"{key} = ${i}")
        values.append(value)
    set_sql = ", ".join(set_clauses)

    row = await pool.fetchrow(
        f"UPDATE life.assets SET {set_sql}, updated_at = now() "
        f"WHERE id = $1 RETURNING {_SELECT_COLS}",
        *values,
    )
    if not row:
        return None
    asset = dict(row)
    await sync_service_due(pool, asset)
    return asset


async def delete_asset(pool: asyncpg.Pool, asset_id: UUID | str) -> bool:
    """Delete by id. Returns False (no exception) when the row didn't exist.

    The machine-generated `asset_service` mirror goes with it: once the asset
    is gone that row could never be refreshed or re-linked (the FK NULLs its
    asset_id), so it would nag forever about something you no longer own.

    Everything else pointing at the asset survives with `asset_id` set to
    NULL — a hand-written warranty row is user data, and the FK is ON DELETE
    SET NULL precisely so this bare DELETE cannot raise ForeignKeyViolation
    and 500 the Assets page.
    """
    await pool.execute(
        "DELETE FROM life.expiring_items WHERE asset_id = $1 AND kind = $2",
        asset_id,
        SERVICE_KIND,
    )
    result = await pool.execute("DELETE FROM life.assets WHERE id = $1", asset_id)
    return result != "DELETE 0"
