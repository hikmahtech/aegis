"""The hub's settle windows, as operator-editable config.

How long a class of problem must persist before it is believed is a fact about
the operator's own estate, not about AEGIS: a homelab where a service takes four
minutes to come back wants a different number from a cluster that recovers in
twenty seconds. So the numbers live in the ``hub_settle_seconds`` settings row,
merged over the generic defaults in ``services/hub.py`` (#537).

This module is the admin surface for that row — the same shape as
``gtd_rules``, ``content_routes`` and ``email_triage_rules``:

* :func:`merge` is the READ, and it is lenient. A malformed entry must never
  stop an alert being handled, so a bad value is dropped and the class falls
  back to its code default.
* :func:`validate` is the WRITE, and it is strict. That same leniency at the
  write boundary would let a typo save with a 200 and then silently do nothing
  forever, which is the worse failure: the operator believes they changed
  something.

One number, two jobs, deliberately: this row also sets how long an
investigation waits before spending effort, because "long enough to believe
this is real" is one question. Zeroing a class therefore also removes its
verification delay — for a class whose alert only fires after its own
self-repair has failed, that is a duplicate check worth removing, and for
anything else it is a trade the operator should make knowingly.
"""

from __future__ import annotations

from typing import Any

from aegis.services.hub import (
    _VERIFY_SECONDS,
    SETTLE_SETTINGS_KEY,
    VERIFY_SECONDS_DEFAULT,
    _slug,
)

# Any class may be named; these are the ones with a non-default code value, so
# the admin page can show what it is overriding rather than a blank form.
DEFAULTS: dict[str, int] = dict(_VERIFY_SECONDS)
WILDCARD = "*"
# A window longer than this is not a settle window, it is a mute with extra
# steps — and a problem nobody hears about for an hour is the failure the hub
# exists to prevent.
MAX_SECONDS = 3600


def merge(value: Any) -> dict[str, int]:
    """The stored row, read leniently: bad entries dropped, never raised."""
    out: dict[str, int] = {}
    if not isinstance(value, dict):
        return out
    for klass, raw in value.items():
        key = WILDCARD if str(klass).strip() == WILDCARD else _slug(str(klass))
        if not key:
            continue
        try:
            out[key] = max(0, min(MAX_SECONDS, int(raw)))
        except (TypeError, ValueError):
            continue
    return out


def validate(raw: Any) -> dict[str, int]:
    """Normalise a row for writing, or raise ValueError (the PUT answers 400)."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("hub_settle_seconds must be an object of class → seconds")
    out: dict[str, int] = {}
    for klass, value in raw.items():
        name = str(klass).strip()
        if not name:
            raise ValueError("a class name cannot be empty")
        key = WILDCARD if name == WILDCARD else _slug(name)
        if not key:
            raise ValueError(f"{name!r} is not a usable class name")
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name}: {value!r} is not a whole number of seconds") from None
        if seconds < 0:
            raise ValueError(f"{name}: seconds cannot be negative")
        if seconds > MAX_SECONDS:
            raise ValueError(
                f"{name}: {seconds}s is longer than the {MAX_SECONDS}s cap — a window that "
                "long is a mute, not a settle window"
            )
        out[key] = seconds
    return out


async def get_settle_seconds(pool: Any) -> dict[str, Any]:
    """What the admin page shows: the effective overrides plus the code
    defaults they sit on, so an operator can see what a blank field means."""
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", SETTLE_SETTINGS_KEY)
    return {
        "overrides": merge(row["value"] if row else None),
        "defaults": dict(sorted(DEFAULTS.items())),
        "default_seconds": VERIFY_SECONDS_DEFAULT,
        "max_seconds": MAX_SECONDS,
        "wildcard": WILDCARD,
    }


async def save_settle_seconds(pool: Any, raw: Any) -> dict[str, Any]:
    """Replace the row (validated); returns what :func:`get_settle_seconds`
    would now return."""
    stored = validate(raw)
    if stored:
        await pool.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
            SETTLE_SETTINGS_KEY,
            stored,
        )
    else:
        # An empty object means "no overrides": delete the row rather than
        # storing `{}`, so the effective config is the code defaults and
        # nothing suggests an override that is not there.
        await pool.execute("DELETE FROM settings WHERE key = $1", SETTLE_SETTINGS_KEY)
    return await get_settle_seconds(pool)
