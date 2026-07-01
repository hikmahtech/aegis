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

logger = structlog.get_logger()

SETTINGS_KEY = "llm_backend"
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
        row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY)
        if row and row["value"]:
            v = row["value"]
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
        logger.warning("llm_backend_read_failed", error=str(exc)[:200])
    if data is None:
        data = _env_backend(settings)
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
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY)
    existing = (row["value"] if row and row["value"] else {}) or {}
    api_key_enc = existing.get("api_key_enc")
    if api_key is not None:
        api_key_enc = encrypt_secret(api_key, settings.secret_key)
    value = {
        "provider": provider,
        "base_url": base_url,
        "tiers": {str(k): str(v) for k, v in (tiers or {}).items()},
        "api_key_enc": api_key_enc or {"value": "", "encrypted": False},
    }
    await pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
        SETTINGS_KEY,
        value,
    )
    invalidate()


def invalidate() -> None:
    _cache.update(data=None, ts=0.0)
