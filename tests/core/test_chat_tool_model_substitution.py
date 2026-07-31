"""Regression test: chat must swap to a tool-capable model when tools are
present and the resolved model can't actually carry a `tools` array -- and
must NOT swap away a model that already can.

Bug caught 2026-05-28: `chat_tool_calls` was empty across all agents for 7
days. Root cause: the Claude-Code-subscription "bridge" (the LiteLLM
aliases claude-haiku/claude-sonnet/claude-opus, served via max-proxy)
silently strips the `tools=` array from upstream requests. Fix: in
`send_message`, when tools_enabled AND the agent has tools AND the resolved
model is one of those three bare bridge aliases (`_TOOL_INCAPABLE_MODELS`,
an exact-match set), substitute the model with whatever the live `balanced`
tier resolves to via `tier_to_model("balanced")`.

Second bug (D1, improvement-observations.md): the fallback used to be a
hardcoded literal (`_TOOL_FALLBACK_MODEL = "gpt-oss:20b"`), whose host went
down indefinitely -- config/models.yaml moved `balanced` to `kimi-k2.5` for
exactly that reason, but the guard's fallback never followed. Fixed to a
live `tier_to_model("balanced")` lookup so the fallback always tracks
whatever's actually configured, with a safe no-op if `balanced` isn't
resolvable (rather than crashing the chat request).

NOTE, because this was gotten wrong once already during the D1 work: the
exact-match set is intentional and correct. `claude-sonnet-5` and
`claude-haiku-4.5` are DIFFERENT LiteLLM model entries -- real Anthropic-API
aliases (infra: ansible/roles/ollama/templates/litellm-config.yaml.j2,
`model_info.supports_function_calling: true`), not the max-proxy bridge --
and `smart` resolves to `claude-sonnet-5` today. A `claude-` prefix check
would also catch those versioned, tool-capable names and silently downgrade
every tool-bearing smart-tier chat turn to `balanced` for no reason. Do not
"fix" this into a prefix check again; the tests below pin both directions.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis.config import Settings
from aegis.llm.tier import set_model_tiers, tier_to_model
from aegis.services.chat import (
    _TOOL_INCAPABLE_MODELS,
    send_message,
)


@pytest.fixture(autouse=True)
def _load_tiers():
    """Set the tiers this module needs before each test, and restore the
    conftest.py session baseline afterward -- tests in this file mutate the
    process-global `_TIERS` map (including deliberately dropping the
    'balanced' key), and that must not leak into other test modules."""
    set_model_tiers(
        {"fast": "gemma4:e2b", "balanced": "kimi-k2.5", "smart": "claude-sonnet-5"}
    )
    yield
    set_model_tiers({"fast": "gemma4:e2b", "balanced": "qwen3:14b", "smart": "qwen3:32b"})


def test_tool_incapable_models_is_exact_bridge_alias_set() -> None:
    """Must stay an exact-match set of the three bare max-proxy bridge
    aliases. Versioned Anthropic-API names must NOT be members -- see the
    module docstring for why a prefix check is wrong here."""
    assert frozenset({"claude-haiku", "claude-sonnet", "claude-opus"}) == _TOOL_INCAPABLE_MODELS
    assert "claude-sonnet-5" not in _TOOL_INCAPABLE_MODELS
    assert "claude-haiku-4.5" not in _TOOL_INCAPABLE_MODELS


def test_tool_fallback_resolves_from_live_balanced_tier_not_a_literal() -> None:
    """The fallback must track whatever config/models.yaml (or the
    DB-configured backend) currently designates as `balanced` -- not a
    hardcoded model name like the old `gpt-oss:20b` literal, which
    config/models.yaml documents as down indefinitely."""
    assert tier_to_model("balanced") == "kimi-k2.5"

    # Reconfigure tiers at runtime (mirrors what happens when the admin UI
    # saves a new LLM backend) and confirm the fallback moves with it.
    set_model_tiers(
        {"fast": "gemma4:e2b", "balanced": "some-other-model", "smart": "claude-sonnet-5"}
    )
    assert tier_to_model("balanced") == "some-other-model"


def _settings(tools_enabled: bool = True) -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost/test",
        litellm_url="https://litellm.test/v1",
        temporal_ui_url="https://temporal.test",
        n8n_ui_url="https://n8n.test",
        admin_username="admin",
        admin_password="admin",
        n8n_webhook_secret="test-secret",
        model_balanced="kimi-k2.5",
        tool_calling_enabled=tools_enabled,
        tool_max_iterations=5,
        tool_result_max_bytes=4096,
        tool_timeout_seconds=30,
    )


def _mock_pool(model_tier: str):
    """Mirror of the mock_pool fixture in test_chat_tools_foundation.py --
    kept inline so this regression test is self-contained."""
    pool = AsyncMock()
    pool.fetchrow.return_value = {
        "id": "pandoras-actor",
        "name": "Pandora's Actor",
        "system_prompt_path": "personalities/pandoras-actor/SOUL.md",
    }
    pool.fetch.return_value = []
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=model_tier)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _mock_llm():
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


@pytest.mark.asyncio
async def test_smart_tier_versioned_claude_model_is_left_unchanged() -> None:
    """Pandora (smart-tier -> claude-sonnet-5, the live config/models.yaml
    value) has tools (trigger_workflow, etc.). claude-sonnet-5 is a real
    Anthropic-API alias with `supports_function_calling: true`
    (litellm-config.yaml.j2), NOT the max-proxy bridge, so send_message must
    call llm_client.chat with the model UNCHANGED. Regression guard against
    re-introducing a `claude-` prefix check, which would silently downgrade
    every tool-bearing smart-tier chat turn to `balanced` for no reason."""
    pool = _mock_pool("smart")
    llm = _mock_llm()

    await send_message(pool, llm, "pandoras-actor", "hello", settings=_settings())

    call_kwargs = llm.chat.call_args[1]
    assert call_kwargs["model"] == "claude-sonnet-5"
    assert call_kwargs.get("tools"), "tools array must still reach the LLM call"


@pytest.mark.asyncio
async def test_bridge_alias_model_substitutes_to_live_balanced_tier() -> None:
    """A resolved model that IS one of the bare max-proxy bridge aliases
    (e.g. a stale/manual 'smart' tier override, or a legacy DB row) must
    still be caught and substituted to whatever `balanced` resolves to
    dynamically -- this is the guard's actual purpose."""
    set_model_tiers(
        {"fast": "gemma4:e2b", "balanced": "kimi-k2.5", "smart": "claude-sonnet"}
    )
    pool = _mock_pool("smart")
    llm = _mock_llm()

    await send_message(pool, llm, "pandoras-actor", "hello", settings=_settings())

    call_kwargs = llm.chat.call_args[1]
    expected = tier_to_model("balanced")
    assert call_kwargs["model"] == expected, (
        f"expected substitution to {expected!r}, got {call_kwargs['model']!r}"
    )
    assert call_kwargs["model"] != "claude-sonnet"
    assert call_kwargs.get("tools"), "tools array must reach the LLM call"


@pytest.mark.asyncio
async def test_balanced_tier_agent_keeps_resolved_model() -> None:
    """The balanced-tier model is already tool-capable -- no substitution."""
    pool = _mock_pool("balanced")
    llm = _mock_llm()

    await send_message(pool, llm, "sebas", "hello", settings=_settings())

    call_kwargs = llm.chat.call_args[1]
    assert call_kwargs["model"] == "kimi-k2.5"


@pytest.mark.asyncio
async def test_no_tools_no_substitution_even_for_smart_tier() -> None:
    """tool_calling_enabled=False -> no substitution, regardless of tier.
    Preserves plain-chat quality for synthesis-heavy work without tools."""
    pool = _mock_pool("smart")
    llm = _mock_llm()

    await send_message(
        pool,
        llm,
        "pandoras-actor",
        "hello",
        settings=_settings(tools_enabled=False),
    )

    call_kwargs = llm.chat.call_args[1]
    assert call_kwargs["model"] == "claude-sonnet-5"
    assert call_kwargs.get("tools") in (None, [])


@pytest.mark.asyncio
async def test_substitution_degrades_safely_when_balanced_tier_unresolvable() -> None:
    """If the resolved model IS a bridge alias but the `balanced` tier isn't
    loaded (e.g. tiers not yet set at boot, or the admin-configured backend
    omits it), the guard must leave the model unchanged rather than crash
    the chat request."""
    set_model_tiers({"smart": "claude-sonnet"})  # bridge alias, no 'balanced' entry
    pool = _mock_pool("smart")
    llm = _mock_llm()

    await send_message(pool, llm, "pandoras-actor", "hello", settings=_settings())

    call_kwargs = llm.chat.call_args[1]
    assert call_kwargs["model"] == "claude-sonnet"
