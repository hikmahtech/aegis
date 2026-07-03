"""Tests for structured system-prompt building from the persona kind dict."""

from aegis.services.chat import _build_agent_system_prompt


def test_builds_prompt_from_persona_dict():
    """soul/agents/user kinds render as the structured prompt sections."""
    prompt = _build_agent_system_prompt(
        "sebas",
        fallback="fallback prompt",
        persona={
            "soul": "I am Sebas, the head butler.",
            "agents": "## Decision Types\nREPLY, REMEMBER, DELEGATE",
            "user": "the owner is the user.",
        },
    )

    assert "## Identity" in prompt
    assert "I am Sebas" in prompt
    assert "## Operational Boundaries" in prompt
    assert "REPLY, REMEMBER, DELEGATE" in prompt
    assert "## User Context" in prompt
    assert "the owner is the user" in prompt


def test_falls_back_to_db_prompt_when_persona_empty():
    """If every kind is empty, use the DB system_prompt fallback."""
    prompt = _build_agent_system_prompt(
        "nonexistent-agent",
        fallback="DB fallback prompt",
        persona={"soul": "", "agents": "", "user": "", "memory": ""},
    )
    assert prompt == "DB fallback prompt"


def test_partial_persona_still_builds_prompt():
    """If only soul is present, prompt has identity section but not others."""
    prompt = _build_agent_system_prompt(
        "raphael", fallback="fallback", persona={"soul": "I am Raphael, the analyst."}
    )

    assert "## Identity" in prompt
    assert "I am Raphael" in prompt
    assert "## Operational Boundaries" not in prompt


def test_memory_kind_rendered_as_memory_section():
    """The memory kind (nee MEMORY.md) is injected as its own section."""
    prompt = _build_agent_system_prompt(
        "maou",
        fallback="fallback",
        persona={"soul": "I am Maou.", "memory": "The owner prefers INR figures."},
    )

    assert "## Memory" in prompt
    assert "The owner prefers INR figures." in prompt


def test_prompt_includes_tool_descriptions():
    """If tool_descriptions is provided, it's included in the prompt."""
    prompt = _build_agent_system_prompt(
        "maou",
        fallback="fallback",
        persona={"soul": "I am Maou, the finance specialist."},
        tool_descriptions="You have: get_quote, get_market_overview",
    )

    assert "## Available Tools" in prompt
    assert "get_quote" in prompt
