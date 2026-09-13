"""The automatic restart's repeat window, as operator-editable config (#501, #558).

A swarm service below its replicas gets one ``docker service update --force``.
If the same problem is back inside the window, it is not restarted again: the
restart did not hold, and the next one will not either. How long "back again
right after the restart" lasts is a fact about the operator's estate, so it
lives in the ``alert_remediation`` settings row::

    {"repeat_window_minutes": 60}

``0`` turns the check off, which restarts every time (the pre-#501 behaviour).

The same shape as ``hub_settle`` and ``email_rules``:

* :func:`merge` is the READ and it is lenient. The worker reads the row through
  it before every restart, and a config mistake must not change what happens to
  a service that is down, so anything that is not a whole number of minutes
  (a bool, a string, a negative number, a row that is not an object) reads as
  the default. It does not clamp: a stored value above :data:`MAX_MINUTES`
  still applies, because the reader has always honoured it.
* :func:`validate` is the WRITE and it is strict. This row gates a mutating
  action, which is the last place a typo should save with a 200 and then do
  nothing, so the PUT answers 400 on an unknown key, a non-integer or a value
  out of range.
"""

from __future__ import annotations

from typing import Any

SETTINGS_KEY = "alert_remediation"
DEFAULT_REPEAT_WINDOW_MINUTES = 60
# A day. A longer window means "a service that broke again tomorrow is not
# restarted either", which is a mute on the auto-restart, not a window.
MAX_MINUTES = 24 * 60
DEFAULTS: dict[str, int] = {"repeat_window_minutes": DEFAULT_REPEAT_WINDOW_MINUTES}


def _minutes(value: Any) -> int | None:
    """A whole, non-negative number of minutes, or None. A bool is not one."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def merge(value: Any) -> dict[str, int]:
    """The stored row over the defaults, read leniently. Never raises."""
    stored = value.get("repeat_window_minutes") if isinstance(value, dict) else None
    minutes = _minutes(stored)
    return {
        "repeat_window_minutes": DEFAULT_REPEAT_WINDOW_MINUTES if minutes is None else minutes
    }


def validate(raw: Any) -> dict[str, int]:
    """Normalise a row for writing, or raise ValueError (the PUT answers 400)."""
    if not isinstance(raw, dict):
        raise ValueError("alert_remediation must be an object with repeat_window_minutes")
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise ValueError(f"unknown key(s): {', '.join(unknown)} — only repeat_window_minutes")
    if "repeat_window_minutes" not in raw:
        raise ValueError("repeat_window_minutes is required (0 turns the check off)")
    value = raw["repeat_window_minutes"]
    minutes = _minutes(value)
    if minutes is None:
        raise ValueError(
            f"repeat_window_minutes: {value!r} is not a whole, non-negative number of minutes"
        )
    if minutes > MAX_MINUTES:
        raise ValueError(
            f"repeat_window_minutes: {minutes} is longer than the {MAX_MINUTES}-minute cap — "
            "a window that long stops the automatic restart, it does not pace it"
        )
    return {"repeat_window_minutes": minutes}


async def get_alert_remediation(pool: Any) -> dict[str, Any]:
    """What the admin page shows: the effective window, the default under it,
    the cap, and whether a row is stored at all."""
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY)
    return {
        **merge(row["value"] if row else None),
        "defaults": dict(DEFAULTS),
        "max_minutes": MAX_MINUTES,
        "stored": row is not None,
    }


async def save_alert_remediation(pool: Any, raw: Any) -> dict[str, Any]:
    """Replace the row (validated); returns what :func:`get_alert_remediation`
    would now return."""
    stored = validate(raw)
    await pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
        SETTINGS_KEY,
        stored,
    )
    return await get_alert_remediation(pool)
