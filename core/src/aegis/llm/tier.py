"""Model-tier resolution for chat and flows.

Loads tier → model mapping from config/models.yaml at startup and exposes
`resolve_model_for_agent` which reads `agents.model_tier` and returns the
fully-qualified model string (e.g. "ollama/qwen3:32b").

Two-level fallback:
  1. NULL/missing tier (agent row absent or tier column is NULL) → 'balanced'.
  2. Unknown non-NULL tier (value not in the loaded tier map) → 'balanced'
     with a structured warning log so operators can spot stale DB values.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
import yaml

logger = structlog.get_logger()

_TIERS: dict[str, str] = {}


def load_model_tiers(path: Path | str) -> dict[str, str]:
    """Parse config/models.yaml and cache the tier map. Returns the map.

    Raises FileNotFoundError if the path does not exist.
    Raises ValueError if the file lacks a top-level `tiers` key.
    """
    p = Path(path)
    with p.open() as fh:
        data: dict[str, Any] = yaml.safe_load(fh) or {}
    tiers = data.get("tiers")
    if not isinstance(tiers, dict) or not tiers:
        raise ValueError(f"{p}: missing or empty `tiers` key")
    _TIERS.clear()
    _TIERS.update({str(k): str(v) for k, v in tiers.items()})
    return dict(_TIERS)


def set_model_tiers(tiers: dict[str, str]) -> dict[str, str]:
    """Replace the in-process tier map (called at boot from the resolved LLM
    backend, and again when the backend is saved from the admin UI)."""
    _TIERS.clear()
    _TIERS.update({str(k): str(v) for k, v in (tiers or {}).items()})
    return dict(_TIERS)


def tier_to_model(tier: str) -> str:
    """Look up a model string by tier name. Raises KeyError on unknown tier."""
    if tier not in _TIERS:
        raise KeyError(f"unknown model tier {tier!r}; known tiers: {sorted(_TIERS)}")
    return _TIERS[tier]


async def resolve_model_for_agent(pool: Any, agent_id: str) -> str:
    """Return the fully-qualified model string for an agent based on its tier.

    Reads `agents.model_tier` from the DB. Falls back to 'balanced' if the
    agent row is missing or the tier column is NULL (level-1 fallback), and
    also falls back to 'balanced' with a warning if the stored tier name is
    not present in the loaded tier map (level-2 fallback).
    """
    async with pool.acquire() as conn:
        tier = await conn.fetchval("SELECT model_tier FROM agents WHERE id = $1", agent_id)
    try:
        return tier_to_model(tier or "balanced")
    except KeyError:
        logger.warning(
            "unknown_model_tier_fallback",
            agent_id=agent_id,
            tier=tier,
            fallback="balanced",
        )
        return tier_to_model("balanced")
