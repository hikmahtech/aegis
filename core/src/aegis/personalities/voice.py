"""Per-agent voice helpers for flow-emitted messages.

Each AEGIS agent has a SOUL.md describing their character. The LLM
system prompt loads those for chat. But many flow-emitted Slack
messages and Todoist comments are *hardcoded strings* — for those the
LLM never sees the input, so the personality only carries through if
the strings themselves are written in-voice.

This module exposes one helper:

- `voice_line(agent_id, event)` — agent-flavoured one-liner for a
  common event type (e.g. "investigation_started", "pr_opened")

Only the Pandora's Actor agent (the alert/infra investigator) currently
emits these flow-side lines. Unknown `(agent_id, event)` combos fall
back to the bare event name so callers never crash.
"""

from __future__ import annotations

# Agent-flavoured one-liners keyed by (agent_id, event). Each value is a
# Python format string accepting **kwargs for context.
_VOICE_LINES: dict[tuple[str, str], str] = {
    # ── Pandora — theatrical, devoted, infrastructure-aware ──
    ("pandoras-actor", "investigation_started"): (
        "🎭 the owner-sama — investigation has begun. Resource: {resource}."
    ),
    ("pandoras-actor", "scoping_started"): (
        "🎭 *gestures dramatically* the owner-sama — I am scoping the ticket. Target: {resource}."
    ),
    ("pandoras-actor", "investigation_actionable"): (
        "🎭 the owner-sama — investigation complete. The findings are actionable."
    ),
    ("pandoras-actor", "investigation_inconclusive"): (
        "🎭 the owner-sama — investigation complete. The evidence is too thin "
        "to call. I shall not invent a root cause."
    ),
    ("pandoras-actor", "investigation_not_actionable"): (
        "🎭 the owner-sama — investigation complete. Nothing to act on here."
    ),
    ("pandoras-actor", "investigation_self_resolved"): (
        "🎭 the owner-sama — the alert self-resolved during verification. Stand down."
    ),
    ("pandoras-actor", "scoping_actionable"): (
        "🎭 the owner-sama — scoping complete. Findings actionable."
    ),
    ("pandoras-actor", "scoping_inconclusive"): (
        "🎭 the owner-sama — scoping complete. The ticket needs a human eye."
    ),
    ("pandoras-actor", "scoping_not_actionable"): (
        "🎭 the owner-sama — scoping complete. This one is out of code scope."
    ),
    ("pandoras-actor", "pr_opened"): (
        "🎭 the owner-sama — fix proposed. {count} pull request(s) staged for your review."
    ),
    ("pandoras-actor", "fix_discarded"): (
        "🎭 As you command, the owner-sama — the proposed fix is discarded."
    ),
}


def voice_line(
    agent_id: str | None,
    event: str,
    overrides: dict[str, str] | None = None,
    **kwargs,
) -> str:
    """Render an agent-flavoured one-liner for an event.

    Resolution order: caller-supplied ``overrides`` (an agent's
    ``metadata.voice_lines`` — ``{event: template}``) → the shipped
    ``_VOICE_LINES`` defaults → the bare event name. Stays pure and sync so the
    alert-investigation workflow can call it (no DB access in a workflow).

    # ponytail: static defaults are the source of truth; threading
    # metadata.voice_lines from the DB into the flow (via an activity that
    # returns them alongside the resolved agent) is deferred until a second
    # agent actually needs flow-side flavour — YAGNI until then.
    """
    template = event
    if overrides and event in overrides:
        template = overrides[event]
    else:
        template = _VOICE_LINES.get((agent_id or "", event), event)
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        return template
