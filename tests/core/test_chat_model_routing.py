"""Verify chat resolves the LLM model via `agents.model_tier`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis.llm.tier import load_model_tiers, resolve_model_for_agent


@pytest.fixture(autouse=True)
def _load_tiers(tmp_path: Path) -> None:
    yml = tmp_path / "models.yaml"
    yml.write_text("tiers:\n  fast: gemma4:e2b\n  balanced: qwen3:14b\n  smart: qwen3:32b\n")
    load_model_tiers(yml)


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
