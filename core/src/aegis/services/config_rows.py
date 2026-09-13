"""One `settings` row as a config object: lenient read, strict write, short cache.

The research lane's knobs (`feeds_config`, `research_config`, `library_config`,
`research_topics_config`) all follow the `email_rules` / `meeting_rules` shape:
a module defines ``DEFAULTS``, a lenient ``merge`` the readers use and a strict
``validate`` the admin PUT uses. This class is the shared get/save/cache half,
so the four modules do not carry four copies of it.

Two rules, the same as the older pairs:

* **`merge` never raises.** A hand-edited or half-written row yields the
  defaults for whatever it got wrong, because a config read must never stop a
  feed being polled or a question being researched.
* **`validate` raises `ValueError`** on the write path, which the route turns
  into a 400 — a typo saved through the admin page must not become a silent
  no-op.

The cache is per process and short (`ttl` seconds), so the worker and core
each see an admin save within half a minute without a restart; ``save``
clears it at once in the process that wrote. Tests call ``clear_cache``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import structlog

logger = structlog.get_logger()

Merge = Callable[[Any], dict]


class SettingsRow:
    def __init__(self, key: str, merge: Merge, validate: Merge, *, ttl: float = 30.0) -> None:
        self.key = key
        self.merge = merge
        self.validate = validate
        self.ttl = ttl
        self._cached: tuple[float, dict] | None = None

    def clear_cache(self) -> None:
        self._cached = None

    async def get(self, pool: Any, *, fresh: bool = False) -> dict:
        """The effective config: the row merged over the defaults. Never raises;
        an unreadable row reads as the defaults (and is logged)."""
        now = time.monotonic()
        if not fresh and self._cached and now - self._cached[0] < self.ttl:
            return dict(self._cached[1])
        value: Any = None
        if pool is not None:
            try:
                value = await pool.fetchval("SELECT value FROM settings WHERE key = $1", self.key)
            except Exception as exc:  # noqa: BLE001 — a config read must never break a run
                logger.warning("config_row_read_failed", key=self.key, error=str(exc)[:200])
        merged = self.merge(value)
        self._cached = (now, dict(merged))
        return merged

    async def save(self, pool: Any, value: Any) -> dict:
        """Validate, persist, return the effective config. Raises ValueError."""
        normalised = self.validate(value)
        await pool.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
            self.key,
            normalised,
        )
        self.clear_cache()
        return await self.get(pool, fresh=True)


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
