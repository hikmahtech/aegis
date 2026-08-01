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

import datetime as dt
import re
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


# ─────────────────────────── passive enrichment (C2) ───────────────────────────
#
# Email and calendar are harvested for WHO the owner is in contact with. That
# means writing information about real third parties, so the filters below are
# deliberately blunt: it is far better to miss a human than to fill the registry
# with `noreply@` senders or with the owner themselves.

_ADDR_RE = re.compile(r"<([^>]+)>")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Local parts that ARE the whole address (`support@…`, `billing@…`) — a role
# mailbox, not a person. Exact match only: `sanjeev.info@` is a human.
_ROLE_LOCALPARTS = frozenset(
    {
        "admin", "alert", "alerts", "billing", "bounce", "bounces", "care",
        "contact", "customercare", "daemon", "help", "hello", "info", "invoice",
        "invoices", "mail", "mailer", "mailer-daemon", "marketing", "news",
        "newsletter", "notification", "notifications", "notify", "postmaster",
        "receipts", "reply", "root", "sales", "service", "support", "team",
        "updates", "webmaster",
    }
)
# Unmistakable machine markers — matched anywhere in the local part, because
# senders bolt ids onto them (`no-reply-1a2b@`, `bounces+xyz@`).
_MACHINE_MARKERS = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "do_not_reply", "mailer-daemon", "bounce", "notification", "automated",
    "auto-confirm", "unsubscribe",
)
# Google's room/resource/shared-calendar pseudo-attendees. These land in an
# event's `attendees` exactly like a person does.
_MACHINE_DOMAIN_SUFFIXES = (
    "calendar.google.com",
    "resource.calendar.google.com",
    "group.calendar.google.com",
)


def parse_contact(raw: str) -> tuple[str, str]:
    """Split a `"Display Name" <a@b.com>` header into (email, display_name).

    Mirrors `aegis_worker.activities.gmail._normalize_sender` for the address
    half — the email comes back lowercased so it is ready for `normalize_aliases`
    and for the `aliases @> ARRAY[$1]` containment probe `find_people` uses.
    """
    raw = (raw or "").strip()
    if not raw:
        return "", ""
    m = _ADDR_RE.search(raw)
    if m:
        email = m.group(1).strip().lower()
        name = raw[: m.start()].strip().strip('"').strip()
    else:
        email, name = raw.lower(), ""
    return email, name


def is_probably_human(email: str) -> bool:
    """False for role mailboxes, machine senders and calendar resources."""
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        return False
    local, _, domain = email.partition("@")
    if any(domain == s or domain.endswith("." + s) for s in _MACHINE_DOMAIN_SUFFIXES):
        return False
    # Strip the plus-address tag before the role-mailbox comparison.
    base = local.split("+", 1)[0]
    if base in _ROLE_LOCALPARTS:
        return False
    return not any(marker in local for marker in _MACHINE_MARKERS)


def name_from_email(email: str) -> str:
    """`john.doe@x.com` → `John Doe`. Only used when nothing better exists."""
    local = (email or "").split("@")[0].split("+", 1)[0]
    words = [w for w in re.split(r"[._\-]+", local) if w]
    return " ".join(w.capitalize() for w in words) or email


async def record_contact(
    pool: asyncpg.Pool,
    email: str,
    display_name: str = "",
    contact_at: dt.datetime | None = None,
    *,
    allow_create: bool = False,
    owner_emails: frozenset[str] | set[str] = frozenset(),
    source: str = "",
) -> str:
    """Fold one observed contact into `life.people`. Returns the outcome tag.

    Outcomes: `skipped_invalid`, `skipped_non_human`, `skipped_owner`,
    `skipped_ambiguous`, `no_match` (nobody matched and creating was not
    allowed), `updated`, `created`.

    Matching is exact, never fuzzy (the C1 rule — two rows for one human is a
    user problem, silently merging two humans is a data-loss bug):
      1. the address as an alias, then
      2. the display name as the person's `name`, and only when it resolves to
         exactly ONE row — that is the enrichment that matters, because it
         teaches an address to a person the user entered by hand.
    Every alias written goes through `normalize_aliases`; skipping it would make
    the new alias invisible to `find_people`'s containment probe.
    """
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        return "skipped_invalid"
    if email in {o.strip().lower() for o in owner_emails}:
        # The owner is not a contact. Google puts the calendar owner in every
        # event's attendees, so without this the registry grows a row for the
        # user themselves on the very first run.
        return "skipped_owner"
    if not is_probably_human(email):
        return "skipped_non_human"

    display_name = (display_name or "").strip()
    matches = await find_people(pool, email)
    if not matches and display_name:
        matches = await find_people(pool, display_name)
    if len(matches) > 1:
        return "skipped_ambiguous"

    if matches:
        person = matches[0]
        patch: dict[str, Any] = {}
        aliases = normalize_aliases([*(person.get("aliases") or []), email])
        if aliases != list(person.get("aliases") or []):
            patch["aliases"] = aliases
        last = person.get("last_contact")
        if contact_at and (last is None or contact_at > last):
            patch["last_contact"] = contact_at
        if patch:
            await update_person(pool, person["id"], patch)
        return "updated"

    if not allow_create:
        return "no_match"

    await create_person(
        pool,
        {
            "name": display_name or name_from_email(email),
            "aliases": [email],
            "last_contact": contact_at,
            "metadata": {"source": source} if source else {},
        },
    )
    return "created"
