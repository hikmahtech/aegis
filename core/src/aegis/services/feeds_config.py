"""Feed health knobs — the `feeds_config` settings row.

What `RssIngestFlow`, the feed stats and the briefing's monthly "drop it?"
line used to carry as module constants (`feeds.FAILING_AFTER` and friends),
now one DB row edited on Admin → Research → Feed health
(`GET/PUT /api/admin/research/feeds-config`). The code defaults are the
values those constants had, so a deployment with no row behaves as before.

    {
      "failing_after": 3,        # fetches in a row that fail before a hub finding
      "recovered_after": 2,      # good fetches in a row before that finding resolves
      "stale_after_days": 30,    # days without a stored entry before `feed_stale`
                                 # (per feed: channels.config.stale_after_days wins)
      "unused_after_days": 90,   # history a feed needs before it can be called unused,
                                 # and the window "used" is measured over
      "stale_review_hour": 3,    # the UTC hour whose hourly run reconciles stale feeds
      "default_ingest": "full"   # channels.config.ingest for a feed that sets none
    }

Read leniently (`merge`), written strictly (`validate`), cached for 30s
(`config_rows.SettingsRow`). The worker reads it through the
`load_feeds_config` activity, because a workflow cannot hit the DB.
"""

from __future__ import annotations

from typing import Any

from aegis.services.config_rows import SettingsRow, as_int, require_int

SETTINGS_KEY = "feeds_config"
INGEST_MODES = ("full", "abstract", "gate")

DEFAULTS: dict[str, Any] = {
    "failing_after": 3,
    "recovered_after": 2,
    "stale_after_days": 30,
    "unused_after_days": 90,
    "stale_review_hour": 3,
    "default_ingest": "full",
}


def merge(value: Any) -> dict:
    """The row over the defaults. Never raises: a bad field keeps its default."""
    v = value if isinstance(value, dict) else {}
    out = dict(DEFAULTS)
    for key in ("failing_after", "recovered_after", "stale_after_days", "unused_after_days"):
        out[key] = as_int(v.get(key), DEFAULTS[key], minimum=1)
    hour = as_int(v.get("stale_review_hour"), DEFAULTS["stale_review_hour"], minimum=0)
    out["stale_review_hour"] = hour if hour <= 23 else DEFAULTS["stale_review_hour"]
    mode = str(v.get("default_ingest") or "").strip().lower()
    out["default_ingest"] = mode if mode in INGEST_MODES else DEFAULTS["default_ingest"]
    return out


def validate(value: Any) -> dict:
    """Strict counterpart to `merge` for the write path. Raises ValueError."""
    if value is not None and not isinstance(value, dict):
        raise ValueError("feeds_config must be an object")
    v = {**DEFAULTS, **(value or {})}
    out: dict[str, Any] = {}
    for key in ("failing_after", "recovered_after", "stale_after_days", "unused_after_days"):
        out[key] = require_int(v, key, minimum=1, maximum=3650)
    out["stale_review_hour"] = require_int(v, "stale_review_hour", minimum=0, maximum=23)
    mode = v.get("default_ingest")
    if not isinstance(mode, str) or mode.strip().lower() not in INGEST_MODES:
        raise ValueError(f"default_ingest must be one of: {', '.join(INGEST_MODES)}")
    out["default_ingest"] = mode.strip().lower()
    return out


ROW = SettingsRow(SETTINGS_KEY, merge, validate)


async def get_feeds_config(pool: Any) -> dict:
    return await ROW.get(pool)


async def save_feeds_config(pool: Any, value: Any) -> dict:
    return await ROW.save(pool, value)
