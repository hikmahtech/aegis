"""#414 — the chat front door must resolve `fast` through the tier map.

Two sites in `services/chat.py` read the RAW `settings.model_fast` field
instead of `tier_to_model("fast")`: the intent router, and the `model_light`
handed to tool executors. A third, `routes/mcp_server.py::_tool_context`,
does the same for the MCP surface.

`settings.model_fast` is the FALLBACK the tier map falls back TO — it is
`AEGIS_MODEL_FAST` from the stack env, and `services/llm_backend.py` only
consults it when the DB row and `config/models.yaml` both fail to answer.
Reading it directly is therefore not "the same value by another name": it is
the stale one. Observed live 2026-09-06, when a release rendered from a stale
homelab checkout left the env on `qwen3.5:9b` while `models.yaml` said
`bedrock-glm-4.7-flash`. Every tier-map consumer was right; only these
env readers followed the stale value.

The tests below install a tier map and a Settings object whose `model_fast`
is a DIFFERENT string, so a site that reads the settings field fails and one
that reads the tier map passes. Each also pins the degrade path: an unloaded
`fast` tier must fall back to the old value, never crash a chat request.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis.config import Settings
from aegis.llm.tier import set_model_tiers
from aegis.services import chat as chat_mod
from aegis.services.chat import classify_intent, send_message

# The tier map's answer, and the stale env value it must beat.
_TIER_FAST = "tier-fast"
_STALE_ENV_FAST = "stale-env-model"


@pytest.fixture(autouse=True)
def _tiers():
    """Restore the conftest session baseline afterwards — `_TIERS` is a
    process global and these tests deliberately drop keys from it."""
    set_model_tiers({"fast": _TIER_FAST, "balanced": "kimi-k2.5", "smart": "claude-sonnet-5"})
    yield
    set_model_tiers({"fast": "gemma4:e2b", "balanced": "qwen3:14b", "smart": "qwen3:32b"})


def _settings() -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost/test",
        litellm_url="https://litellm.test/v1",
        temporal_ui_url="https://temporal.test",
        n8n_ui_url="https://n8n.test",
        admin_username="admin",
        admin_password="admin",
        n8n_webhook_secret="test-secret",
        model_fast=_STALE_ENV_FAST,
        model_balanced="kimi-k2.5",
        tool_calling_enabled=True,
        tool_max_iterations=5,
        tool_result_max_bytes=4096,
        tool_timeout_seconds=30,
    )


def _router_llm():
    """`think()` recording its model; the reply names no routable agent, so
    `classify_intent` returns the default. What is under test is the model
    the call was made WITH, not the verdict."""
    llm = AsyncMock()
    llm.think = AsyncMock(return_value={"response": '{"agent_id": "sebas"}'})
    return llm


def _chat_pool(model_tier: str = "balanced"):
    pool = AsyncMock()
    pool.fetchrow.return_value = {
        "id": "sebas",
        "name": "Sebas",
        "system_prompt_path": "personalities/sebas/SOUL.md",
    }
    pool.fetch.return_value = []
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=model_tier)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _chat_llm():
    llm = AsyncMock()
    llm.chat = AsyncMock(
        return_value={
            "response": "ok",
            "tool_calls": [],
            "model": "kimi-k2.5",
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }
    )
    return llm


def _capture_tool_context(monkeypatch) -> list:
    """Record every `ToolContext` `send_message` builds, without changing it."""
    built: list = []
    real = chat_mod.ToolContext

    def _recording(*args, **kwargs):
        ctx = real(*args, **kwargs)
        built.append(ctx)
        return ctx

    monkeypatch.setattr(chat_mod, "ToolContext", _recording)
    return built


# A message no keyword in `_INTENT_KEYWORDS` matches, so routing falls through
# to the LLM — which is the only branch that picks a model at all.
_UNROUTABLE = "zzz qqq vvv"


@pytest.mark.asyncio
async def test_intent_routing_calls_the_fast_tier_model_not_the_env_field():
    llm = _router_llm()
    await classify_intent(_UNROUTABLE, llm, _settings(), pool=None)
    assert llm.think.await_count == 1
    assert llm.think.await_args[1]["model"] == _TIER_FAST
    assert llm.think.await_args[1]["model"] != _STALE_ENV_FAST


@pytest.mark.asyncio
async def test_intent_routing_falls_back_to_settings_when_no_fast_tier_is_loaded():
    """Tiers are installed at boot. A request that arrives before that, or a
    backend whose map omits `fast`, must still route — the old value is the
    fallback, and a KeyError escaping here would break the front door."""
    set_model_tiers({"balanced": "kimi-k2.5"})
    llm = _router_llm()
    out = await classify_intent(_UNROUTABLE, llm, _settings(), pool=None)
    assert llm.think.await_args[1]["model"] == _STALE_ENV_FAST
    assert out["agent_id"] == "sebas"


@pytest.mark.asyncio
async def test_model_light_is_the_fast_tier_model_not_the_env_field(monkeypatch):
    """`ToolContext.model_light` is the model chat tools synthesise with — the
    same bypass, one layer further in. (`research_topic` used it until #509
    moved its synthesis onto `ResearchFlow` and the smart tier.)"""
    built = _capture_tool_context(monkeypatch)
    await send_message(_chat_pool(), _chat_llm(), "sebas", "hello", settings=_settings())
    assert built, "send_message built no ToolContext"
    assert built[-1].model_light == _TIER_FAST
    assert built[-1].model_light != _STALE_ENV_FAST


@pytest.mark.asyncio
async def test_model_light_falls_back_to_settings_when_no_fast_tier_is_loaded(monkeypatch):
    set_model_tiers({"balanced": "kimi-k2.5"})
    built = _capture_tool_context(monkeypatch)
    await send_message(_chat_pool(), _chat_llm(), "sebas", "hello", settings=_settings())
    assert built[-1].model_light == _STALE_ENV_FAST


def test_the_mcp_tool_context_resolves_model_light_the_same_way():
    """The third site, found by the issue's own grep. The MCP surface builds
    its own `ToolContext` and had the identical `getattr(settings,
    "model_fast", ...)` read, so an MCP `research_topic` synthesised on the
    stale env model while the same tool over chat used the tier map."""
    from aegis.api.routes.mcp_server import _tool_context

    request = MagicMock()
    request.app.state = MagicMock()
    ctx = _tool_context(request, "sebas", _settings())
    assert ctx.model_light == _TIER_FAST

    set_model_tiers({"balanced": "kimi-k2.5"})
    assert _tool_context(request, "sebas", _settings()).model_light == _STALE_ENV_FAST
