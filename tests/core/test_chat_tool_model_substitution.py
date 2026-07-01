"""Regression test: chat must swap to a tool-capable model when tools are
present and the resolved model is one that strips tools at the proxy layer.

Bug caught 2026-05-28: `chat_tool_calls` was empty across all agents for 7
days. Root cause: max-proxy (which serves claude-haiku/sonnet/opus via the
Claude-Code-subscription bridge) silently strips the `tools=` array from
upstream requests. Every smart-tier agent had tools defined but the model
never saw them, so it responded with plain text or confabulated that no
tools were available. Fix: in `send_message`, when tools_enabled AND the
agent has tools AND the resolved model is in `_TOOL_INCAPABLE_MODELS`,
substitute the model with `_TOOL_FALLBACK_MODEL` (qwen3:14b, which has
`supports_function_calling: true` in the LiteLLM proxy config).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis.config import Settings
from aegis.llm.tier import load_model_tiers
from aegis.services.chat import (
    _TOOL_FALLBACK_MODEL,
    _TOOL_INCAPABLE_MODELS,
    send_message,
)


@pytest.fixture(autouse=True)
def _load_tiers(tmp_path: Path) -> None:
    yml = tmp_path / "models.yaml"
    yml.write_text(
        "tiers:\n"
        "  fast: gemma4:e2b\n"
        "  balanced: qwen3:14b\n"
        "  smart: claude-sonnet\n"
    )
    load_model_tiers(yml)


def test_tool_incapable_models_includes_all_max_proxy_aliases() -> None:
    """Pin the set so a future addition (e.g. claude-haiku-4-2) is intentional."""
    assert frozenset(
        {"claude-haiku", "claude-sonnet", "claude-opus"}
    ) == _TOOL_INCAPABLE_MODELS


def test_tool_fallback_model_is_function_calling_capable() -> None:
    """gpt-oss:20b is the primary function-calling-capable model in the LiteLLM
    proxy config (`supports_function_calling: true` in
    `infra-gitops/ansible/roles/ollama/templates/litellm-config.yaml.j2`).
    qwen3:14b is the configured router fallback if Ollama can't serve gpt-oss."""
    assert _TOOL_FALLBACK_MODEL == "gpt-oss:20b"


def _settings(tools_enabled: bool = True) -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost/test",
        litellm_url="https://litellm.test/v1",
        temporal_ui_url="https://temporal.test",
        n8n_ui_url="https://n8n.test",
        admin_username="admin",
        admin_password="admin",
        n8n_webhook_secret="test-secret",
        model_balanced="qwen3:14b",
        tool_calling_enabled=tools_enabled,
        tool_max_iterations=5,
        tool_result_max_bytes=4096,
        tool_timeout_seconds=30,
    )


def _mock_pool(model_tier: str):
    """Mirror of the mock_pool fixture in test_chat_tools_foundation.py — kept
    inline so this regression test is self-contained."""
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
            "model": "qwen3:14b",
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }
    )
    return llm


@pytest.mark.asyncio
async def test_smart_tier_agent_with_tools_substitutes_to_fallback() -> None:
    """Pandora (smart-tier → claude-sonnet) has tools (trigger_workflow, etc.).
    send_message must call llm_client.chat with model=_TOOL_FALLBACK_MODEL
    (gpt-oss:20b), NOT claude-sonnet."""
    pool = _mock_pool("smart")
    llm = _mock_llm()

    await send_message(
        pool, llm, "pandoras-actor", "hello", settings=_settings()
    )

    call_kwargs = llm.chat.call_args[1]
    assert call_kwargs["model"] == _TOOL_FALLBACK_MODEL, (
        f"expected substitution to {_TOOL_FALLBACK_MODEL}, got {call_kwargs['model']}"
    )
    assert call_kwargs.get("tools"), "tools array must reach the LLM call"


@pytest.mark.asyncio
async def test_balanced_tier_agent_keeps_resolved_model() -> None:
    """qwen3:14b (balanced tier) is already tool-capable — no substitution."""
    pool = _mock_pool("balanced")
    llm = _mock_llm()

    await send_message(pool, llm, "sebas", "hello", settings=_settings())

    call_kwargs = llm.chat.call_args[1]
    assert call_kwargs["model"] == "qwen3:14b"


@pytest.mark.asyncio
async def test_no_tools_no_substitution_even_for_smart_tier() -> None:
    """tool_calling_enabled=False → no substitution, regardless of tier.
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
    assert call_kwargs["model"] == "claude-sonnet"
    assert call_kwargs.get("tools") in (None, [])
