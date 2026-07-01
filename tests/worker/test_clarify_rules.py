"""Rule-dict lookups encoded in clarify._RULES."""

from __future__ import annotations

from aegis_worker.activities.clarify import _RULES


def test_default_assignee_by_source_tag() -> None:
    assert _RULES.default_assignee("#email") == "@sebas"
    assert _RULES.default_assignee("#alert") == "@pandora"
    assert _RULES.default_assignee("#receipt") == "@maou"
    assert _RULES.default_assignee("#research") == "@raphael"
    assert _RULES.default_assignee("#calendar") == "@sebas"
    assert _RULES.default_assignee("#manual") == "@me"
    assert _RULES.default_assignee("#chat") == "@me"
    # Unknown source falls back to @me
    assert _RULES.default_assignee("#unknown") == "@me"
    assert _RULES.default_assignee(None) == "@me"


def test_default_contexts_by_source_tag() -> None:
    assert _RULES.default_contexts("#email") == ["@email", "@5min"]
    assert _RULES.default_contexts("#alert") == ["@code", "@deep"]
    assert _RULES.default_contexts("#research") == ["@reading"]
    assert _RULES.default_contexts("#unknown") == ["@deep"]
    assert _RULES.default_contexts(None) == ["@deep"]


def test_skip_inbox_research_routes_to_reference() -> None:
    assert _RULES.skip_inbox("#research") == "reference"
    assert _RULES.skip_inbox("#email") is None
    assert _RULES.skip_inbox(None) is None
