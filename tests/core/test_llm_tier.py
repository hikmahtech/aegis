"""Unit tests for the model-tier resolver."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import aegis.llm.tier as _tier_mod
import pytest
from aegis.llm.tier import load_model_tiers, resolve_model_for_agent, tier_to_model


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


def test_load_model_tiers_reads_yaml(tmp_path: Path) -> None:
    yml = tmp_path / "models.yaml"
    yml.write_text(
        dedent(
            """
            tiers:
              fast:     "ollama/gemma3:4b"
              balanced: "ollama/qwen3:14b"
              smart:    "ollama/qwen3:32b"
            """
        )
    )
    tiers = load_model_tiers(yml)
    assert tiers == {
        "fast": "ollama/gemma3:4b",
        "balanced": "ollama/qwen3:14b",
        "smart": "ollama/qwen3:32b",
    }


def test_load_model_tiers_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_model_tiers(tmp_path / "does-not-exist.yaml")


def test_load_model_tiers_missing_tiers_key_raises(tmp_path: Path) -> None:
    yml = tmp_path / "models.yaml"
    yml.write_text("litellm: {keep_alive: 10m}\n")
    with pytest.raises(ValueError, match="tiers"):
        load_model_tiers(yml)


def test_tier_to_model_known_tier(tmp_path: Path) -> None:
    yml = tmp_path / "models.yaml"
    yml.write_text("tiers: {fast: 'ollama/x', balanced: 'ollama/y', smart: 'ollama/z'}\n")
    load_model_tiers(yml)
    assert tier_to_model("balanced") == "ollama/y"


def test_tier_to_model_unknown_tier_raises(tmp_path: Path) -> None:
    yml = tmp_path / "models.yaml"
    yml.write_text("tiers: {fast: 'a', balanced: 'b', smart: 'c'}\n")
    load_model_tiers(yml)
    with pytest.raises(KeyError, match="mystery"):
        tier_to_model("mystery")


@pytest.mark.asyncio
async def test_resolve_model_for_agent_maps_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yml = tmp_path / "models.yaml"
    yml.write_text("tiers: {fast: 'ollama/fast', balanced: 'ollama/bal', smart: 'ollama/smart'}\n")
    load_model_tiers(yml)

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
async def test_resolve_model_for_agent_unknown_agent_uses_balanced(tmp_path: Path) -> None:
    yml = tmp_path / "models.yaml"
    yml.write_text("tiers: {fast: 'ollama/fast', balanced: 'ollama/bal', smart: 'ollama/smart'}\n")
    load_model_tiers(yml)

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
async def test_resolve_model_for_agent_unknown_tier_falls_back_to_balanced(tmp_path: Path) -> None:
    yml = tmp_path / "models.yaml"
    yml.write_text("tiers: {fast: 'ollama/fast', balanced: 'ollama/bal', smart: 'ollama/smart'}\n")
    load_model_tiers(yml)

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
