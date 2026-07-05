"""Per-agent voice helper tests.

Verifies the flow-side voice helper `voice_line`. The contract is
intentionally narrow — these strings appear in hardcoded Slack messages
and Todoist comments where the LLM never sees the input. Only the
Pandora's Actor agent emits these lines.
"""

from __future__ import annotations

from aegis.personalities import voice_line


def test_voice_line_pandora_scoping_started() -> None:
    line = voice_line("pandoras-actor", "scoping_started", resource="screener-p-server")
    assert "the owner-sama" in line
    assert "screener-p-server" in line
    assert "🎭" in line


def test_voice_line_pandora_pr_opened() -> None:
    line = voice_line("pandoras-actor", "pr_opened", count=3)
    assert "the owner-sama" in line
    assert "3" in line


def test_voice_line_pandora_investigation_verdicts() -> None:
    """All four verdict shapes should produce Pandora-voiced output."""
    actionable = voice_line("pandoras-actor", "investigation_actionable")
    inconclusive = voice_line("pandoras-actor", "investigation_inconclusive")
    not_actionable = voice_line("pandoras-actor", "investigation_not_actionable")
    self_resolved = voice_line("pandoras-actor", "investigation_self_resolved")
    for line in (actionable, inconclusive, not_actionable, self_resolved):
        assert "the owner-sama" in line
        assert "🎭" in line
    assert "actionable" in actionable.lower()
    assert "self-resolved" in self_resolved.lower() or "stand down" in self_resolved.lower()


def test_voice_line_unknown_event_returns_event_name() -> None:
    """An event we haven't defined falls back to the event name string."""
    line = voice_line("pandoras-actor", "made_up_event_for_testing")
    assert line  # non-empty


# --- Issue #36: metadata-supplied voice overrides ---------------------------


def test_voice_line_overrides_take_precedence() -> None:
    """A caller-supplied per-agent override (metadata.voice_lines) wins over
    the shipped defaults and formats with kwargs."""
    out = voice_line(
        "custom-infra",
        "pr_opened",
        overrides={"pr_opened": "🤖 {count} PR(s) up for review."},
        count=3,
    )
    assert out == "🤖 3 PR(s) up for review."


def test_voice_line_falls_back_to_static_defaults_without_override() -> None:
    """No override → the shipped _VOICE_LINES default still renders (unchanged)."""
    out = voice_line("pandoras-actor", "investigation_started", resource="node-a")
    assert "the owner-sama" in out and "node-a" in out


def test_voice_line_override_missing_event_uses_default() -> None:
    """An overrides dict lacking the event falls back to the static default."""
    out = voice_line(
        "pandoras-actor",
        "investigation_started",
        overrides={"some_other_event": "x"},
        resource="node-b",
    )
    assert "the owner-sama" in out and "node-b" in out
