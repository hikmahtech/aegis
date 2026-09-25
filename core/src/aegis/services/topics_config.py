"""Tracked-topic thresholds — the `research_topics_config` settings row.

How many items a topic's round must hold, per priority, before it interrupts
the user with a task (`research_topics.Topic.threshold_for`), and how many
items a round's task and digest list. Edited on Admin → Research → Topic
thresholds (`GET/PUT /api/admin/research/topics-config`); a topic's own
`threshold` in the registry overrides the per-priority number.

    {"attention": {"high": 2, "medium": 3, "low": 5}, "digest_items": 10,
     "brief_items": 7, "weekly_day": 6}

The defaults are what `ATTENTION_ITEMS` and the three copies of 10 said.
`brief_items` caps the area stories (#674) in one morning brief across all
areas; `weekly_day` (0 = Monday … 6 = Sunday, on the user's clock) is the day
weekly and vault areas get their digest.
"""

from __future__ import annotations

from typing import Any

from aegis.services.config_rows import SettingsRow, as_int, require_int

SETTINGS_KEY = "research_topics_config"
PRIORITIES = ("high", "medium", "low")

DEFAULT_ATTENTION: dict[str, int] = {"high": 2, "medium": 3, "low": 5}
DEFAULTS: dict[str, Any] = {
    "attention": dict(DEFAULT_ATTENTION),
    "digest_items": 10,
    "brief_items": 7,
    "weekly_day": 6,
}


def merge(value: Any) -> dict:
    v = value if isinstance(value, dict) else {}
    raw = v.get("attention") if isinstance(v.get("attention"), dict) else {}
    attention = {
        p: as_int(raw.get(p), DEFAULT_ATTENTION[p], minimum=1) for p in PRIORITIES
    }
    weekly_day = as_int(v.get("weekly_day"), DEFAULTS["weekly_day"], minimum=0)
    return {
        "attention": attention,
        "digest_items": as_int(v.get("digest_items"), DEFAULTS["digest_items"], minimum=1),
        "brief_items": as_int(v.get("brief_items"), DEFAULTS["brief_items"], minimum=1),
        "weekly_day": weekly_day if weekly_day <= 6 else DEFAULTS["weekly_day"],
    }


def validate(value: Any) -> dict:
    if value is not None and not isinstance(value, dict):
        raise ValueError("research_topics_config must be an object")
    v = {**DEFAULTS, **(value or {})}
    raw = v.get("attention")
    if not isinstance(raw, dict):
        raise ValueError("attention must be an object of {priority: items}")
    attention = {**DEFAULT_ATTENTION, **raw}
    for key in attention:
        if key not in PRIORITIES:
            raise ValueError(f"attention has an unknown priority {key!r}")
    out = {p: require_int(attention, p, minimum=1, maximum=1000) for p in PRIORITIES}
    return {
        "attention": out,
        "digest_items": require_int(v, "digest_items", minimum=1, maximum=100),
        "brief_items": require_int(v, "brief_items", minimum=1, maximum=50),
        "weekly_day": require_int(v, "weekly_day", minimum=0, maximum=6),
    }


ROW = SettingsRow(SETTINGS_KEY, merge, validate)


async def get_topics_config(pool: Any) -> dict:
    return await ROW.get(pool)


async def save_topics_config(pool: Any, value: Any) -> dict:
    return await ROW.save(pool, value)
