"""Verify chat resolves the LLM model via `agents.model_tier`."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis.llm.tier import resolve_model_for_agent, set_model_tiers


@pytest.fixture(autouse=True)
def _load_tiers() -> None:
    set_model_tiers({"fast": "gemma4:e2b", "balanced": "qwen3:14b", "smart": "qwen3:32b"})


@pytest.mark.asyncio
async def test_raphael_resolves_to_smart_tier() -> None:
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value="smart")

    class FakePool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(self):
                    return conn

                async def __aexit__(self, *a):
                    return False

            return _Ctx()

    model = await resolve_model_for_agent(FakePool(), "raphael")
    assert model == "qwen3:32b"


@pytest.mark.asyncio
async def test_sebas_resolves_to_balanced_tier() -> None:
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value="balanced")

    class FakePool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(self):
                    return conn

                async def __aexit__(self, *a):
                    return False

            return _Ctx()

    model = await resolve_model_for_agent(FakePool(), "sebas")
    assert model == "qwen3:14b"
