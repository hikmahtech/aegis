"""Knowledge ranking — the `knowledge_ranking` settings row.

How a chat turn ranks what the knowledge store finds before it goes into the
prompt (`chat._gather_knowledge_context`): the lift an agent's own
`metadata.knowledge_domains` get, and per source type a ranking multiplier and
a decay window. Edited on Admin → Knowledge → Ranking
(`GET/PUT /api/admin/knowledge/ranking`).

    {
      "domain_boost": 0.2,          # added to similarity for the agent's own domains
      "source_types": {             # a PARTIAL override of services/source_types.py
        "note":  {"rank_boost": 1.25, "decay_days": 3650},
        "email": {"decay_days": 30}  # a key left out keeps the registry's value
      }
    }

A result's score is `(similarity + domain boost) * decay * rank_boost`, and the
threshold and the order both read it.

`source_types` merges over `SOURCE_TYPES`: with no row, or for a type the row
does not name, a document ranks exactly as the registry says. `decay_days:
null` means the default window (`DEFAULT_DECAY_DAYS`), as a registry type with
no window of its own has. A type the registry does not know may be named too:
`source_type` is free text, and agents' domains name types such as `sentry`.

The score is computed per search result by sync code with no pool, so the row
is read once per turn by the async caller (`get_ranking`, 30s cache) and handed
down as a `Ranking`. Nothing here does I/O per result.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from aegis.services.config_rows import SettingsRow
from aegis.services.source_types import (
    DEFAULT_DECAY_DAYS,
    SOURCE_TYPES,
    get_decay_days,
    get_rank_boost,
)

SETTINGS_KEY = "knowledge_ranking"
DEFAULT_DOMAIN_BOOST = 0.2
DEFAULTS: dict[str, Any] = {"domain_boost": DEFAULT_DOMAIN_BOOST, "source_types": {}}

MAX_DOMAIN_BOOST = 1.0
MAX_RANK_BOOST = 10.0
MAX_DECAY_DAYS = 36500
# A source type as the ingest paths write it: lowercase, digits, `_ . -`.
_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_ENTRY_KEYS = ("rank_boost", "decay_days")


def _number(raw: Any) -> float | None:
    """`raw` as a finite float, or None (bools, strings, NaN and inf are not numbers)."""
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    n = float(raw)
    return n if math.isfinite(n) else None


def _rank_boost(raw: Any) -> float | None:
    n = _number(raw)
    return n if n is not None and 0.0 <= n <= MAX_RANK_BOOST else None


def _decay_days(raw: Any) -> tuple[bool, int | None]:
    """(ok, value): None is a valid value (the default window)."""
    if raw is None:
        return True, None
    n = _number(raw)
    if n is None or n != int(n) or not 1 <= n <= MAX_DECAY_DAYS:
        return False, None
    return True, int(n)


def merge(value: Any) -> dict:
    """Lenient: the row over the defaults. Anything malformed is dropped, so a
    hand-edited row can never stop a chat turn finding its context."""
    v = value if isinstance(value, dict) else {}
    boost = _number(v.get("domain_boost"))
    out: dict[str, Any] = {
        "domain_boost": (
            boost if boost is not None and 0.0 <= boost <= MAX_DOMAIN_BOOST else DEFAULT_DOMAIN_BOOST
        )
    }
    types: dict[str, dict[str, Any]] = {}
    raw_types = v.get("source_types") if isinstance(v.get("source_types"), dict) else {}
    for name, raw in raw_types.items():
        if not isinstance(name, str) or not _TYPE_RE.match(name) or not isinstance(raw, dict):
            continue
        entry: dict[str, Any] = {}
        if "rank_boost" in raw and (rb := _rank_boost(raw["rank_boost"])) is not None:
            entry["rank_boost"] = rb
        if "decay_days" in raw:
            ok, days = _decay_days(raw["decay_days"])
            if ok:
                entry["decay_days"] = days
        if entry:
            types[name] = entry
    out["source_types"] = types
    return out


def validate(value: Any) -> dict:
    """Strict: raises ValueError (the route's 400) on anything merge would drop."""
    if value is not None and not isinstance(value, dict):
        raise ValueError("knowledge_ranking must be an object")
    v = {**DEFAULTS, **(value or {})}
    boost = _number(v.get("domain_boost"))
    if boost is None or not 0.0 <= boost <= MAX_DOMAIN_BOOST:
        raise ValueError(f"domain_boost must be a number from 0 to {MAX_DOMAIN_BOOST}")
    raw_types = v.get("source_types")
    if not isinstance(raw_types, dict):
        raise ValueError("source_types must be an object of source type → settings")
    types: dict[str, dict[str, Any]] = {}
    for name, raw in raw_types.items():
        if not isinstance(name, str) or not _TYPE_RE.match(name):
            raise ValueError(
                f"source type {name!r} must be lowercase letters, digits, '_', '.' or '-' "
                "(at most 64 characters)"
            )
        if not isinstance(raw, dict):
            raise ValueError(f"source_types.{name} must be an object")
        unknown = sorted(set(raw) - set(_ENTRY_KEYS))
        if unknown:
            raise ValueError(f"source_types.{name}: unknown setting(s) {', '.join(unknown)}")
        entry: dict[str, Any] = {}
        if "rank_boost" in raw:
            rb = _rank_boost(raw["rank_boost"])
            if rb is None:
                raise ValueError(
                    f"source_types.{name}.rank_boost must be a number from 0 to {MAX_RANK_BOOST}"
                )
            entry["rank_boost"] = rb
        if "decay_days" in raw:
            ok, days = _decay_days(raw["decay_days"])
            if not ok:
                raise ValueError(
                    f"source_types.{name}.decay_days must be a whole number of days from 1 "
                    f"to {MAX_DECAY_DAYS}, or null for the default {DEFAULT_DECAY_DAYS}"
                )
            entry["decay_days"] = days
        if entry:
            types[name] = entry
    return {"domain_boost": boost, "source_types": types}


ROW = SettingsRow(SETTINGS_KEY, merge, validate)


@dataclass(frozen=True)
class Ranking:
    """The effective ranking knobs for one turn: the row merged over the registry."""

    domain_boost: float = DEFAULT_DOMAIN_BOOST
    source_types: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> Ranking:
        return cls(
            domain_boost=float(cfg.get("domain_boost", DEFAULT_DOMAIN_BOOST)),
            source_types=dict(cfg.get("source_types") or {}),
        )

    def decay_days(self, source_type: str) -> int:
        override = self.source_types.get(source_type) or {}
        if "decay_days" in override:
            return override["decay_days"] or DEFAULT_DECAY_DAYS
        return get_decay_days(source_type)

    def rank_boost(self, source_type: str) -> float:
        override = self.source_types.get(source_type) or {}
        if "rank_boost" in override:
            return float(override["rank_boost"])
        return get_rank_boost(source_type)


# The registry alone: what a turn uses with no row, and what a caller with no
# pool (a unit test, a sync helper) gets.
DEFAULT_RANKING = Ranking()


def registry_view() -> dict[str, dict[str, Any]]:
    """The shipped registry's per-type values, for the admin page's placeholders."""
    return {
        name: {
            "rank_boost": info.rank_boost,
            "decay_days": info.decay_days,
            "description": info.description,
        }
        for name, info in sorted(SOURCE_TYPES.items())
    }


async def get_ranking_config(pool: Any) -> dict:
    return await ROW.get(pool)


async def save_ranking_config(pool: Any, value: Any) -> dict:
    return await ROW.save(pool, value)


async def get_ranking(pool: Any) -> Ranking:
    """The effective ranking. Never raises: an unreadable row is the defaults."""
    return Ranking.from_config(await ROW.get(pool))
