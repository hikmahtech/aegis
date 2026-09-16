"""The two `settings` statements, written once.

AEGIS keeps its operator-editable config in one table — `settings(key, value
jsonb, updated_at)` — and reads or writes it from about fifty modules across
core and the worker. Every one of them used to spell out the same SELECT and
the same upsert, so a change to how the row is stored (a codec, a column, an
`updated_at` rule) had eighty-odd places to reach.

Two functions, no cache and no policy:

* :func:`get_setting` returns the stored value, or None when there is no row.
  `value` is NOT NULL, so None means "unset" and nothing else.
* :func:`put_setting` stores a value, replacing whatever was there.

Neither swallows an error: a caller that must not fail on a config read keeps
its own ``try``/``except`` around the call, which is where the decision
belongs. ``pool`` is anything asyncpg-shaped — a Pool, a Connection inside a
transaction, or a test double — because plenty of callers read the row while
holding a connection.

The lenient-read / strict-write pair that most config rows want is
:class:`aegis.services.config_rows.SettingsRow`, which is built on these.
"""

from __future__ import annotations

from typing import Any

_SELECT = "SELECT value FROM settings WHERE key = $1"
_UPSERT = (
    "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
    "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()"
)


async def get_setting(pool: Any, key: str) -> Any:
    """The value stored under `key`, or None when the row does not exist."""
    return await pool.fetchval(_SELECT, key)


async def put_setting(pool: Any, key: str, value: Any) -> None:
    """Store `value` under `key` (insert or replace), stamping `updated_at`."""
    await pool.execute(_UPSERT, key, value)
