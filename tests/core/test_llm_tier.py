"""Unit tests for the model-tier resolver."""

from __future__ import annotations

import aegis.llm.tier as _tier_mod
import pytest
from aegis.llm.tier import resolve_model_for_agent, set_model_tiers, tier_to_model


@pytest.fixture(autouse=True)
def _clear_tiers():
    """Clear the module-level tier cache before and after every test to prevent
    test-order coupling. The session-scoped _load_model_tiers_for_tests fixture
    pre-populates _TIERS once; save and restore it so teardown doesn't wipe the
    session baseline for later tests."""
    saved = dict(_tier_mod._TIERS)
    _tier_mod._TIERS.clear()
    yield
    _tier_mod._TIERS.clear()
    _tier_mod._TIERS.update(saved)


def test_set_model_tiers_replaces_map() -> None:
    set_model_tiers({"fast": "ollama/gemma3:4b", "balanced": "ollama/qwen3:14b"})
    tiers = set_model_tiers({"fast": "a", "balanced": "b", "smart": "c"})
    assert tiers == {"fast": "a", "balanced": "b", "smart": "c"}
    assert tier_to_model("smart") == "c"


def test_tier_to_model_known_tier() -> None:
    set_model_tiers({"fast": "ollama/x", "balanced": "ollama/y", "smart": "ollama/z"})
    assert tier_to_model("balanced") == "ollama/y"


def test_tier_to_model_unknown_tier_raises() -> None:
    set_model_tiers({"fast": "a", "balanced": "b", "smart": "c"})
    with pytest.raises(KeyError, match="mystery"):
        tier_to_model("mystery")


@pytest.mark.asyncio
async def test_resolve_model_for_agent_maps_tier() -> None:
    set_model_tiers({"fast": "ollama/fast", "balanced": "ollama/bal", "smart": "ollama/smart"})

    class FakeConn:
        async def fetchval(self, sql: str, *args):
            assert "model_tier" in sql
            assert args == ("raphael",)
            return "smart"

    class FakePool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(self):
                    return FakeConn()

                async def __aexit__(self, *a):
                    return False

            return _Ctx()

    model = await resolve_model_for_agent(FakePool(), "raphael")
    assert model == "ollama/smart"


@pytest.mark.asyncio
async def test_resolve_model_for_agent_unknown_agent_uses_balanced() -> None:
    set_model_tiers({"fast": "ollama/fast", "balanced": "ollama/bal", "smart": "ollama/smart"})

    class FakeConn:
        async def fetchval(self, sql: str, *args):
            return None  # agent not found

    class FakePool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(self):
                    return FakeConn()

                async def __aexit__(self, *a):
                    return False

            return _Ctx()

    model = await resolve_model_for_agent(FakePool(), "ghost-agent")
    assert model == "ollama/bal"


@pytest.mark.asyncio
async def test_resolve_model_for_agent_unknown_tier_falls_back_to_balanced() -> None:
    set_model_tiers({"fast": "ollama/fast", "balanced": "ollama/bal", "smart": "ollama/smart"})

    class FakeConn:
        async def fetchval(self, sql: str, *args):
            return "premium"  # unknown tier not in the map

    class FakePool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(self):
                    return FakeConn()

                async def __aexit__(self, *a):
                    return False

            return _Ctx()

    model = await resolve_model_for_agent(FakePool(), "some-agent")
    assert model == "ollama/bal"
