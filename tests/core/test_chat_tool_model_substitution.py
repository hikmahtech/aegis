"""Regression test: chat must swap to a tool-capable model when tools are
present and the resolved model can't actually carry a `tools` array.

Bug caught 2026-05-28: `chat_tool_calls` was empty across all agents for 7
days. Root cause: the Claude-Code-subscription bridge (serving the LiteLLM
aliases claude-haiku/sonnet/opus) silently strips the `tools=` array from
upstream requests. Fix: in `send_message`, when tools_enabled AND the agent
has tools AND the resolved model starts with `_TOOL_INCAPABLE_MODEL_PREFIX`
("claude-"), substitute the model with whatever the live `balanced` tier
resolves to via `tier_to_model("balanced")`.

Second bug (D1, improvement-observations.md): the guard originally matched
by EXACT equality against the three bare alias names
({"claude-haiku", "claude-sonnet", "claude-opus"}), and the fallback was a
hardcoded literal ("gpt-oss:20b", whose host later went down indefinitely).
Once config/models.yaml pointed `smart` at a *versioned* name
("claude-sonnet-5"), the exact-match guard silently stopped firing at all.
Fixed to a prefix match + a live `balanced`-tier lookup so neither bug can
recur independently of what's configured today.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis.config import Settings
from aegis.llm.tier import set_model_tiers, tier_to_model
from aegis.services.chat import (
    _TOOL_INCAPABLE_MODEL_PREFIX,
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


def test_tool_incapable_prefix_matches_bare_and_versioned_claude_names() -> None:
    """Prefix match must catch both the old bare bridge aliases and any
    versioned Anthropic-API name a tier might resolve to."""
    assert _TOOL_INCAPABLE_MODEL_PREFIX == "claude-"
    for name in ("claude-haiku", "claude-sonnet", "claude-opus", "claude-sonnet-5"):
        assert name.startswith(_TOOL_INCAPABLE_MODEL_PREFIX)


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
async def test_smart_tier_agent_with_versioned_claude_model_substitutes_to_balanced() -> None:
    """Pandora (smart-tier -> claude-sonnet-5, the live config/models.yaml
    value) has tools (trigger_workflow, etc.). send_message must call
    llm_client.chat with model=tier_to_model("balanced"), NOT claude-sonnet-5.
    This is the exact scenario the D1 fix targets: a *versioned* smart-tier
    model name that the old exact-match guard silently failed to catch."""
    pool = _mock_pool("smart")
    llm = _mock_llm()

    await send_message(pool, llm, "pandoras-actor", "hello", settings=_settings())

    call_kwargs = llm.chat.call_args[1]
    expected = tier_to_model("balanced")
    assert call_kwargs["model"] == expected, (
        f"expected substitution to {expected!r}, got {call_kwargs['model']!r}"
    )
    assert call_kwargs["model"] != "claude-sonnet-5"
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
    """If the `balanced` tier isn't loaded (e.g. tiers not yet set at boot,
    or the admin-configured backend omits it), the guard must leave the
    model unchanged rather than crash the chat request."""
    set_model_tiers({"smart": "claude-sonnet-5"})  # no 'balanced' entry
    pool = _mock_pool("smart")
    llm = _mock_llm()

    await send_message(pool, llm, "pandoras-actor", "hello", settings=_settings())

    call_kwargs = llm.chat.call_args[1]
    assert call_kwargs["model"] == "claude-sonnet-5"
