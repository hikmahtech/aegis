"""The user's own clock: the `user_timezone` settings row.

One reader for the setting, so a date a person reads — a note's dated
heading, the time on a journal note Raphael creates, "today" in a chat tool —
is their calendar date and not the server's. The containers and the Postgres
session run in UTC, which is the user's yesterday for the first five and a
half hours of an IST day.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import structlog

logger = structlog.get_logger()

SETTING = "user_timezone"


async def user_zone(pool: Any) -> ZoneInfo:
    """The user's timezone.

    Never raises: no pool, no row, a value that is not a zone name or a failed
    read all give UTC — a typo'd setting must not take a caller down. The
    pool's jsonb codec decodes the stored scalar, so `"Asia/Kolkata"` arrives
    as the bare zone name."""
    if pool is not None:
        try:
            row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", SETTING)
            name = row["value"] if row else None
            if isinstance(name, str) and name.strip():
                return ZoneInfo(name.strip())
        except Exception as exc:  # noqa: BLE001 — never break a caller on a config read
            logger.warning("user_timezone_read_failed", error=str(exc)[:200])
    return ZoneInfo("UTC")


async def user_now(pool: Any) -> datetime:
    """Now on the user's clock, timezone-aware."""
    return datetime.now(await user_zone(pool))
