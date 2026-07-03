"""Personalities as DB data: persona-dict prompt build + starter-file reader."""

from __future__ import annotations

from aegis.services.chat import _build_agent_system_prompt


def test_persona_all_kinds_render_sections():
    out = _build_agent_system_prompt(
        "zz-no-such-agent",
        fallback="FB",
        persona={"soul": "I am Z", "agents": "I do X", "user": "owner likes Y", "memory": "M1"},
    )
    assert "## Identity" in out and "I am Z" in out
    assert "## Operational Boundaries" in out and "I do X" in out
    assert "## User Context" in out and "owner likes Y" in out
    assert "## Memory" in out and "M1" in out


def test_persona_empty_returns_fallback():
    out = _build_agent_system_prompt("zz-no-such-agent", fallback="FB", persona={})
    assert out == "FB"


def test_persona_partial_only_includes_present_sections():
    out = _build_agent_system_prompt(
        "zz-no-such-agent", fallback="FB", persona={"soul": "only soul"}
    )
    assert "## Identity" in out and "only soul" in out
    assert "## Operational Boundaries" not in out and "## User Context" not in out
    assert "## Memory" not in out


def test_persona_tools_appended():
    out = _build_agent_system_prompt(
        "zz-no-such-agent", fallback="FB", persona={"soul": "S"}, tool_descriptions="tool docs"
    )
    assert "## Available Tools" in out and "tool docs" in out


def test_read_personality_files_reads_shipped_sebas():
    from aegis.services.personalities import read_personality_files

    kinds = read_personality_files("sebas")
    # The shipped example personas include SOUL.md + USER.md + MEMORY.md;
    # AGENTS.md (operating notes) is intentionally omitted from the public examples.
    assert kinds.get("soul") and kinds.get("user") and kinds.get("memory")
    assert "agents" not in kinds
