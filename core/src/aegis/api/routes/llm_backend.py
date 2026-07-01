"""Admin: configurable LLM backend (Phase A — bring-your-own key + backend).

GET returns the active config (never the key — only `api_key_set`). PUT saves it
and live-reloads core's client + tier map. POST /test does a tiny call against
the given (or saved) config so the user can verify before relying on it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.llm import LLMClient, set_model_tiers
from aegis.services.llm_backend import (
    PROVIDER_PRESETS,
    get_llm_backend,
    save_llm_backend,
)

router = APIRouter(prefix="/api/admin/llm-backend", dependencies=[Depends(verify_auth)])


@router.get("")
async def get_backend(
    request: Request, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    backend = await get_llm_backend(request.app.state.db_pool, settings, use_cache=False)
    return {
        "provider": backend["provider"],
        "base_url": backend["base_url"],
        "tiers": backend["tiers"],
        "api_key_set": bool(backend["api_key"]),
        "source": backend["source"],
        "presets": PROVIDER_PRESETS,
    }


@router.put("")
async def put_backend(
    request: Request, body: dict[str, Any], settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    pool = request.app.state.db_pool
    # api_key: omitted → keep existing; "" → clear; a value → set (write-only).
    api_key = body.get("api_key") if "api_key" in body else None
    await save_llm_backend(
        pool,
        settings,
        provider=body.get("provider", "custom"),
        base_url=body.get("base_url", ""),
        tiers=body.get("tiers", {}),
        api_key=api_key,
    )
    # Live-reload core's client + tier map (the worker picks up on next restart).
    backend = await get_llm_backend(pool, settings, use_cache=False)
    set_model_tiers(backend["tiers"])
    request.app.state.llm = LLMClient(
        base_url=backend["base_url"], api_key=backend["api_key"], timeout=settings.litellm_timeout
    )
    request.app.state.llm_backend = backend
    return {"ok": True, "tiers": backend["tiers"], "api_key_set": bool(backend["api_key"])}


@router.post("/test")
async def test_backend(
    request: Request, body: dict[str, Any], settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    saved = await get_llm_backend(request.app.state.db_pool, settings, use_cache=False)
    base_url = body.get("base_url") or saved["base_url"]
    api_key = body.get("api_key") or saved["api_key"]
    tiers = body.get("tiers") or saved["tiers"]
    model = tiers.get("fast") or tiers.get("balanced") or next(iter(tiers.values()), "")
    if not base_url or not model:
        return {"ok": False, "error": "missing base_url or a tier model"}
    client = LLMClient(base_url=base_url, api_key=api_key, timeout=30)
    try:
        result = await client.think(
            "Reply with the single word: ok",
            model=model,
            max_tokens=50,
            purpose="backend_test",
        )
        text = result.get("response", "") if isinstance(result, dict) else str(result)
        return {"ok": True, "model": model, "reply": (text or "").strip()[:120]}
    except Exception as exc:  # noqa: BLE001 — surface the failure to the UI
        return {"ok": False, "error": str(exc)[:300]}
    finally:
        await client.close()
