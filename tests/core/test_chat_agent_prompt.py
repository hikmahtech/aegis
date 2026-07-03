"""Tests for structured prompt building from personality files."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from aegis.services.chat import _build_agent_system_prompt


def test_builds_prompt_from_personality_files():
    """Loads SOUL.md, AGENTS.md, USER.md and builds structured prompt."""
    with tempfile.TemporaryDirectory() as tmp:
        agent_dir = Path(tmp) / "sebas"
        agent_dir.mkdir()
        (agent_dir / "SOUL.md").write_text("I am Sebas, the head butler.")
        (agent_dir / "AGENTS.md").write_text("## Decision Types\nREPLY, REMEMBER, DELEGATE")
        (agent_dir / "USER.md").write_text("the owner is the user.")

        with patch("aegis.services.chat.PERSONALITY_DIR", tmp):
            prompt = _build_agent_system_prompt("sebas", fallback="fallback prompt")

    assert "## Identity" in prompt
    assert "I am Sebas" in prompt
    assert "## Operational Boundaries" in prompt
    assert "REPLY, REMEMBER, DELEGATE" in prompt
    assert "## User Context" in prompt
    assert "the owner is the user" in prompt


def test_falls_back_to_db_prompt_when_no_files():
    """If personality dir doesn't exist, use the DB system_prompt."""
    with tempfile.TemporaryDirectory() as tmp, patch("aegis.services.chat.PERSONALITY_DIR", tmp):
        prompt = _build_agent_system_prompt("nonexistent-agent", fallback="DB fallback prompt")

    assert prompt == "DB fallback prompt"


def test_partial_files_still_builds_prompt():
    """If only SOUL.md exists, prompt has identity section but not others."""
    with tempfile.TemporaryDirectory() as tmp:
        agent_dir = Path(tmp) / "raphael"
        agent_dir.mkdir()
        (agent_dir / "SOUL.md").write_text("I am Raphael, the analyst.")

        with patch("aegis.services.chat.PERSONALITY_DIR", tmp):
            prompt = _build_agent_system_prompt("raphael", fallback="fallback")

    assert "## Identity" in prompt
    assert "I am Raphael" in prompt
    assert "## Operational Boundaries" not in prompt


def test_prompt_includes_tool_descriptions():
    """If tool_descriptions is provided, it's included in the prompt."""
    with tempfile.TemporaryDirectory() as tmp:
        agent_dir = Path(tmp) / "maou"
        agent_dir.mkdir()
        (agent_dir / "SOUL.md").write_text("I am Maou, the finance specialist.")

        with patch("aegis.services.chat.PERSONALITY_DIR", tmp):
            prompt = _build_agent_system_prompt(
                "maou",
                fallback="fallback",
                tool_descriptions="You have: get_quote, get_market_overview",
            )

    assert "## Available Tools" in prompt
    assert "get_quote" in prompt
