"""Configurable LLM backend (Phase A) — bring-your-own key + backend.

The active backend (provider, base_url, api_key, tier→model map) lives in the
``settings`` table under ``llm_backend``, edited from the admin UI. It falls back
to the maintainer's env (``litellm_url`` / ``litellm_api_key`` / the
``model_*`` settings / ``config/models.yaml``) when unset — so existing
deployments keep working until config is moved into the UI.

Both core and worker build their ``LLMClient`` + tier map from here at boot;
core rebuilds on UI save, the worker picks up on its next restart. The resolved
tier map is cached briefly so model tweaks propagate without a restart.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import structlog
import yaml

from aegis.crypto import decrypt_secret, encrypt_secret
from aegis.errors import error_text
from aegis.llm.routes import merge_routes, set_routes
from aegis.llm.tier import set_model_tiers
from aegis.services.settings_store import get_setting, put_setting

logger = structlog.get_logger()

SETTINGS_KEY = "llm_backend"
# Purpose → category → model routing (see `aegis/llm/routes.py`). A partial
# override in this `settings` row is merged OVER the `routes:` block in
# config/models.yaml, so the DB can retune one category without restating the
# file. There is no admin UI for it yet — write the row directly.
ROUTES_SETTINGS_KEY = "llm_routes"
_CACHE_TTL = 30.0
_cache: dict[str, Any] = {"data": None, "ts": 0.0}

# Provider presets — the UI prefills base_url; the user supplies key + per-tier
# model. One OpenAI-compatible endpoint per backend (a proxy fronts multi-provider).
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "litellm": {"label": "LiteLLM proxy", "base_url": ""},
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1"},
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1"},
    "anthropic": {"label": "Anthropic", "base_url": "https://api.anthropic.com/v1"},
    "ollama": {"label": "Ollama (local)", "base_url": "http://localhost:11434/v1"},
    "custom": {"label": "Custom (OpenAI-compatible)", "base_url": ""},
}


def install_llm_config(backend: dict[str, Any]) -> None:
    """Install a resolved backend's tier map AND its routing table.

    Three sites resolve a backend — core's boot, the worker's, and the admin
    save that re-resolves it live — and all three must install both halves:
    refreshing the tiers while leaving an edited `llm_routes` row behind is a
    half-applied backend, which is how a route change once stayed invisible
    to core until a restart.

    A routing table that will not validate is logged and routing is turned
    off. It never blocks a boot or fails a save.
    """
    set_model_tiers(backend["tiers"])
    logger.info(
        "model_tiers_loaded",
        tiers=sorted(backend["tiers"]),
        source=backend.get("source", ""),
    )
    try:
        routes = set_routes(backend.get("routes"))
    except Exception as exc:  # noqa: BLE001 — a bad routing table must not block boot
        set_routes(None)
        logger.warning("llm_routes_invalid", error=error_text(exc))
        return
    logger.info(
        "llm_routes_loaded",
        categories=len(routes["categories"]),
        purposes=len(routes["purposes"]),
    )


def _env_tiers(settings: Any) -> dict[str, str]:
    try:
        data = yaml.safe_load(Path(settings.models_yaml_path).read_text()) or {}
        tiers = data.get("tiers") or {}
        if tiers:
            return {str(k): str(v) for k, v in tiers.items()}
    except Exception:
        pass
    # OSS low-footprint: no models.yaml → derive from the model_* settings.
    return {
        "fast": settings.model_fast,
        "balanced": settings.model_balanced,
        "smart": settings.model_smart,
    }


def _env_routes(settings: Any) -> dict[str, Any]:
    """The `routes:` block of config/models.yaml, or `{}` when absent.

    Unreadable file, no `routes:` key, wrong shape — all resolve to "no
    routing", which is the pre-routing behaviour. Never raises: a fork with no
    models.yaml must still boot.
    """
    try:
        data = yaml.safe_load(Path(settings.models_yaml_path).read_text()) or {}
        routes = data.get("routes")
        if isinstance(routes, dict) and routes:
            return {
                "categories": dict(routes.get("categories") or {}),
                "purposes": dict(routes.get("purposes") or {}),
            }
    except Exception:
        pass
    return {}


async def _db_routes(pool: Any) -> dict[str, Any] | None:
    """The `llm_routes` settings row, or None. Never raises — a bad config read
    must not stop the process booting."""
    try:
        value = await get_setting(pool, ROUTES_SETTINGS_KEY)
        if value:
            if isinstance(value, str):  # pool without a jsonb codec
                import json

                value = json.loads(value)
            if isinstance(value, dict):
                return value
    except Exception as exc:  # noqa: BLE001 — never break boot on a config read
        logger.warning("llm_routes_read_failed", error=error_text(exc))
    return None


def _env_backend(settings: Any) -> dict[str, Any]:
    return {
        "provider": "litellm",
        "base_url": settings.litellm_url,
        "api_key": settings.litellm_api_key,
        "tiers": _env_tiers(settings),
        "source": "env",
    }


async def get_llm_backend(pool: Any, settings: Any, *, use_cache: bool = True) -> dict[str, Any]:
    """Resolve the active backend: DB ``settings.llm_backend`` if present, else env."""
    now = time.monotonic()
    if use_cache and _cache["data"] is not None and now - _cache["ts"] < _CACHE_TTL:
        return _cache["data"]
    data: dict[str, Any] | None = None
    try:
        v = await get_setting(pool, SETTINGS_KEY)
        if v:
            api_key = decrypt_secret(v.get("api_key_enc"), settings.secret_key)
            tiers = {str(k): str(val) for k, val in (v.get("tiers") or {}).items()}
            data = {
                "provider": v.get("provider", "custom"),
                "base_url": v.get("base_url") or settings.litellm_url,
                "api_key": api_key or settings.litellm_api_key,
                "tiers": tiers or _env_tiers(settings),
                "source": "db",
            }
    except Exception as exc:  # noqa: BLE001 — never break boot on a config read
        logger.warning("llm_backend_read_failed", error=error_text(exc))
    if data is None:
        data = _env_backend(settings)
    # Routing is independent of which backend won: the yaml block is the base
    # and the `llm_routes` row is a partial override on top of it, whether the
    # models/keys came from the DB or the env.
    data["routes"] = merge_routes(_env_routes(settings), await _db_routes(pool))
    _cache.update(data=data, ts=now)
    return data


async def save_llm_backend(
    pool: Any,
    settings: Any,
    *,
    provider: str,
    base_url: str,
    tiers: dict[str, str],
    api_key: str | None = None,
) -> None:
    """Upsert the backend. ``api_key=None`` keeps the existing key (write-only field)."""
    existing = (await get_setting(pool, SETTINGS_KEY)) or {}
    api_key_enc = existing.get("api_key_enc")
    if api_key is not None:
        api_key_enc = encrypt_secret(api_key, settings.secret_key)
    value = {
        "provider": provider,
        "base_url": base_url,
        "tiers": {str(k): str(v) for k, v in (tiers or {}).items()},
        "api_key_enc": api_key_enc or {"value": "", "encrypted": False},
    }
    await put_setting(pool, SETTINGS_KEY, value)
    invalidate()


def invalidate() -> None:
    _cache.update(data=None, ts=0.0)
