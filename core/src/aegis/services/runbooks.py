"""Per-alert runbooks stored as data (the `runbooks` table, migration 044, #499).

Pandora puts a runbook in front of every alert investigation
(``AlertActivities.gather_alert_knowledge``). Lookup order:

1. this table — runbooks about one deployment's own setup, written by the
   operator over ``/api/admin/runbooks`` or the admin **Runbooks** page;
2. ``<runbooks_dir>/<AlertName>.md`` — the generic runbooks that ship in the
   repo's ``runbooks/`` directory and are baked into the worker image.

The repo is public, so nothing about the operator's machines belongs in
``runbooks/``. It belongs here.

A runbook is keyed on its alert name folded to lowercase letters and digits
(``normalise_name``). Alert names reach AEGIS in several spellings — a
Prometheus alertname (``NodeDown``), a Grafana rule title (``Dagster Pipeline
Failure``), the problem hub's slug of either (``dagster-pipeline-failure``) —
and one runbook has to answer all of them.

Write is strict and read is lenient, the same split as ``email_rules``: a save
that would store a runbook that can never be served (empty, a stub, too large
to prepend to a prompt, a name with no letters) answers 400; a read never
raises for a bad name, it just finds nothing. A database error on read is left
to the caller, because only the caller knows what to fall back to.
"""

from __future__ import annotations

import re
from typing import Any

# Every runbook is prepended to an investigation prompt, so a size cap is a
# prompt-budget cap. The longest generic runbook in the repo is under 3,000
# characters; 16,000 leaves room for a detailed one without letting a pasted
# log dump crowd out the alert itself.
MAX_BODY_CHARS = 16_000
MAX_NAME_CHARS = 200
MAX_UPDATED_BY_CHARS = 200

# The marker the placeholder files in runbooks/ carry. A stub is no runbook,
# whichever store it comes from.
STUB_MARKER = "TODO: fill in"

_NOT_KEY = re.compile(r"[^0-9a-z]+")


def normalise_name(name: Any) -> str:
    """The lookup key for an alert name: lowercase letters and digits only.

    "NodeDown", "node-down", "Node Down" and "node_down" are all "nodedown".
    Anything that is not a string, or has no letter or digit, gives "".
    """
    if not isinstance(name, str):
        return ""
    return _NOT_KEY.sub("", name.casefold())


def is_stub(body: str) -> bool:
    return STUB_MARKER in body


def validate(name: Any, body: Any) -> tuple[str, str, str]:
    """Return ``(name_key, name, body)`` ready to store, or raise ValueError."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    name = name.strip()
    if len(name) > MAX_NAME_CHARS:
        raise ValueError(f"name is longer than {MAX_NAME_CHARS} characters")
    key = normalise_name(name)
    if not key:
        raise ValueError("name must contain at least one letter or digit")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("body is required and cannot be blank")
    body = body.strip()
    if len(body) > MAX_BODY_CHARS:
        raise ValueError(
            f"body is {len(body)} characters; the limit is {MAX_BODY_CHARS}, because "
            "every runbook is prepended to an investigation prompt"
        )
    if is_stub(body):
        raise ValueError(
            f"body contains the stub marker {STUB_MARKER!r}, so it would never be "
            "served; write the runbook or delete it"
        )
    return key, name, body


async def get_runbook(pool: Any, name: Any) -> dict | None:
    """The stored runbook for an alert name in any spelling, or None.

    A name with no key finds nothing: the table's CHECK keeps '' out of it."""
    row = await pool.fetchrow(
        "SELECT name_key, name, body, updated_at, updated_by FROM runbooks WHERE name_key = $1",
        normalise_name(name),
    )
    return dict(row) if row else None


async def list_runbooks(pool: Any) -> list[dict]:
    """Every stored runbook, by name, without the bodies."""
    rows = await pool.fetch(
        "SELECT name_key, name, length(body) AS chars, updated_at, updated_by "
        "FROM runbooks ORDER BY name_key"
    )
    return [dict(r) for r in rows]


async def put_runbook(pool: Any, name: Any, body: Any, *, updated_by: str) -> dict:
    """Create or replace the runbook for an alert name. Raises ValueError on bad input.

    A second save under another spelling of the same name replaces the same
    row, and the spelling it was saved under becomes the displayed name.
    The result carries ``created`` (True for a new row).
    """
    key, name, body = validate(name, body)
    who = str(updated_by or "").strip()[:MAX_UPDATED_BY_CHARS]
    row = await pool.fetchrow(
        "INSERT INTO runbooks (name_key, name, body, updated_at, updated_by) "
        "VALUES ($1, $2, $3, now(), $4) "
        "ON CONFLICT (name_key) DO UPDATE SET name = EXCLUDED.name, body = EXCLUDED.body, "
        "updated_at = now(), updated_by = EXCLUDED.updated_by "
        # xmax is 0 only on a freshly inserted tuple: the usual upsert tell.
        "RETURNING name_key, name, body, updated_at, updated_by, (xmax = 0) AS created",
        key,
        name,
        body,
        who,
    )
    return dict(row)


async def delete_runbook(pool: Any, name: Any) -> bool:
    """Delete the runbook for an alert name in any spelling. True if one existed."""
    status = await pool.execute("DELETE FROM runbooks WHERE name_key = $1", normalise_name(name))
    return status.endswith(" 1")
