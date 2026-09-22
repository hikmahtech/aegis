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


def test_an_agent_with_tools_is_told_to_state_only_what_a_tool_returned():
    """#640: an agent with list_nodes stated a node's state without calling
    it, and offered a drain it had no tool for. The rule is in code, so a
    persona cannot drop it, and it comes after the tools it is about."""
    prompt = _build_agent_system_prompt(
        "pandoras-actor",
        fallback="fallback",
        persona={"soul": "I am Pandora."},
        tool_descriptions="You have: list_nodes",
    )
    assert "## Evidence" in prompt
    assert prompt.index("## Available Tools") < prompt.index("## Evidence")
    assert "only when a tool returned it" in prompt
    assert "I have not checked" in prompt
    assert "Offer only actions one of your tools can carry out" in prompt


def test_the_evidence_rule_reaches_an_agent_with_no_persona():
    prompt = _build_agent_system_prompt(
        "zz", fallback="DB prompt", persona={}, tool_descriptions="You have: x"
    )
    assert prompt.startswith("DB prompt\n\n## Evidence")


def test_no_tools_no_evidence_rule():
    prompt = _build_agent_system_prompt("zz", fallback="DB prompt", persona={"soul": "I am Z."})
    assert "## Evidence" not in prompt
