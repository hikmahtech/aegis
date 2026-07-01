"""Boot-time validation of AGENT_TOOL_SETS → TOOL_EXECUTORS consistency."""

from __future__ import annotations

import pytest
import structlog.testing
from aegis.services import chat


def test_validator_passes_on_clean_config():
    """Real module state must pass — otherwise production would refuse to boot."""
    chat._validate_agent_tool_sets()  # no exception


def test_validator_raises_on_orphan_reference(monkeypatch: pytest.MonkeyPatch):
    """An AGENT_TOOL_SETS entry with no matching TOOL_EXECUTORS key raises."""
    broken = dict(chat.AGENT_TOOL_SETS)
    broken["synthetic_test_agent"] = {"fictional_tool"}
    monkeypatch.setattr(chat, "AGENT_TOOL_SETS", broken)
    with pytest.raises(RuntimeError, match="fictional_tool"):
        chat._validate_agent_tool_sets()


def test_validator_logs_warning_for_unused_executor(monkeypatch: pytest.MonkeyPatch):
    """Executor present but not referenced by any agent logs chat_tool_unused."""
    extra_executors = dict(chat.TOOL_EXECUTORS)

    async def _ghost(pool, args, ctx):
        return {}

    extra_executors["ghost_tool"] = _ghost
    monkeypatch.setattr(chat, "TOOL_EXECUTORS", extra_executors)
    with structlog.testing.capture_logs() as log_entries:
        chat._validate_agent_tool_sets()
    assert any(
        e.get("event") == "chat_tool_unused" and e.get("tool") == "ghost_tool" for e in log_entries
    )
