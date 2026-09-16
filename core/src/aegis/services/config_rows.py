"""One `settings` row as a config object: lenient read, strict write, short cache.

Nearly every operator-editable knob in AEGIS is one `settings` row with the
same three parts: a module defines its defaults, a lenient ``merge`` the
readers use and a strict ``validate`` the admin PUT uses. This class is the
shared get/save/cache half, so no module carries its own copy of it.

Two rules, which are the reason each module keeps its own ``merge`` and
``validate`` rather than sharing a generic one — those are the domain rules,
this class is only the plumbing:

* **`merge` never raises.** A hand-edited or half-written row yields the
  defaults for whatever it got wrong, because a config read must never stop a
  feed being polled or a question being researched.
* **`validate` raises `ValueError`** on the write path, which the route turns
  into a 400 — a typo saved through the admin page must not become a silent
  no-op.

A row's value is usually an object, but it may be a list (`content_routes`,
`email_task_links` are ordered, first-match-wins rules), so the cache copies
whatever shape ``merge`` returned.

The cache is per process and short (`ttl` seconds), so the worker and core
each see an admin save within half a minute without a restart; ``save``
clears it at once in the process that wrote. Tests call ``clear_cache``, or
``clear_all_caches`` for every row at once.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import structlog

from aegis.errors import error_text
from aegis.services.settings_store import get_setting, put_setting

logger = structlog.get_logger()

Merge = Callable[[Any], Any]

#: Every row built in this process, so tests can clear the lot in one call.
_ROWS: list[SettingsRow] = []

#: Returned by `SettingsRow._read` when the row could not be read AT ALL — the
#: query failed, or there is no pool. Deliberately distinct from None, which
#: means "there is no such row": both merge to the defaults, but only the
#: second is an answer worth caching.
_UNREADABLE: Any = object()


def _copy(value: Any) -> Any:
    """A shallow copy of a merged value, so a caller cannot mutate the cache."""
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return dict(value)
    return value


class SettingsRow:
    def __init__(self, key: str, merge: Merge, validate: Merge, *, ttl: float = 30.0) -> None:
        self.key = key
        self.merge = merge
        self.validate = validate
        self.ttl = ttl
        self._cached: tuple[float, Any] | None = None
        _ROWS.append(self)

    def clear_cache(self) -> None:
        self._cached = None

    async def _read(self, pool: Any) -> Any:
        """The stored value, None for no row, :data:`_UNREADABLE` when the read
        itself failed (or there was no pool). Never raises."""
        if pool is None:
            return _UNREADABLE
        try:
            return await get_setting(pool, self.key)
        except Exception as exc:  # noqa: BLE001 — a config read must never break a run
            logger.warning("config_row_read_failed", key=self.key, error=error_text(exc))
            return _UNREADABLE

    async def raw(self, pool: Any) -> Any:
        """The stored value, unmerged, or None when there is no row.

        For the handful of admin views that have to show the operator's
        overrides beside the effective config — a merged read cannot tell
        "stored the default" from "stored nothing". Never cached, and — unlike
        :meth:`get` — it does NOT swallow a failed read: a form that reports
        what is stored must say the database was unreachable, not answer
        "nothing is".
        """
        return await get_setting(pool, self.key)

    async def get(self, pool: Any, *, fresh: bool = False) -> Any:
        """The effective config: the row merged over the defaults. Never raises;
        an unreadable row reads as the defaults (and is logged).

        A failed read is answered but NOT cached. These rows are read on hot
        paths — every mail classified, every task clarified — and caching a
        blip's answer would hold "no sender overrides" for `ttl` seconds after
        the database came back, which is how a run silently loses the
        `financial`/`payments` tags an override carries. The next call retries.
        """
        now = time.monotonic()
        if not fresh and self._cached and now - self._cached[0] < self.ttl:
            return _copy(self._cached[1])
        value = await self._read(pool)
        if value is _UNREADABLE:
            return self.merge(None)
        merged = self.merge(value)
        self._cached = (now, _copy(merged))
        return merged

    async def save(self, pool: Any, value: Any) -> Any:
        """Validate, persist, return the effective config. Raises ValueError."""
        normalised = self.validate(value)
        await put_setting(pool, self.key, normalised)
        self.clear_cache()
        return await self.get(pool, fresh=True)

    async def delete(self, pool: Any) -> None:
        """Remove the row, so the effective config is the code defaults and
        nothing in the form suggests an override that is not there."""
        await pool.execute("DELETE FROM settings WHERE key = $1", self.key)
        self.clear_cache()


def clear_all_caches() -> None:
    """Drop every row's cache. For tests: a row written straight to the database
    rather than through ``save`` is otherwise invisible for up to `ttl` seconds."""
    for row in _ROWS:
        row.clear_cache()


def as_int(value: Any, default: int, *, minimum: int | None = None) -> int:
    """`value` as an int, or `default` when it is not one (or under `minimum`)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if isinstance(value, bool):
        return default
    if minimum is not None and n < minimum:
        return default
    return n


def as_float(value: Any, default: float, *, minimum: float | None = None) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if isinstance(value, bool):
        return default
    if minimum is not None and n < minimum:
        return default
    return n


def require_int(v: dict, key: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    """The strict half: `v[key]` must be an int inside the bounds. Raises ValueError."""
    raw = v.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int | float) or int(raw) != raw:
        raise ValueError(f"{key} must be a whole number")
    n = int(raw)
    if minimum is not None and n < minimum:
        raise ValueError(f"{key} must be at least {minimum}")
    if maximum is not None and n > maximum:
        raise ValueError(f"{key} must be at most {maximum}")
    return n


def require_float(v: dict, key: str, *, minimum: float, maximum: float) -> float:
    raw = v.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise ValueError(f"{key} must be a number")
    n = float(raw)
    if n < minimum or n > maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return n


def str_list(value: Any) -> list[str]:
    """Lenient: the non-empty strings in a list, stripped; anything else is []."""
    if not isinstance(value, list):
        return []
    return [s.strip() for s in value if isinstance(s, str) and s.strip()]


def require_str_list(v: dict, key: str) -> list[str]:
    raw = v.get(key)
    if not isinstance(raw, list) or not all(isinstance(s, str) for s in raw):
        raise ValueError(f"{key} must be a list of strings")
    return [s.strip() for s in raw if s.strip()]
