"""The transition table (`hub.decide`) and event validation. Pure: no DB."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from aegis.services.hub import Event, decide, validate_event

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _p(status: str, resolved_ago: timedelta | None = None) -> dict:
    return {
        "id": "p1",
        "status": status,
        "resolved_at": (NOW - resolved_ago) if resolved_ago else None,
        "muted_until": None,
        "occurrences": 3,
    }


@pytest.mark.parametrize(
    ("current", "kind", "action", "status"),
    [
        # occurrence
        (None, "occurrence", "create", "open"),
        (_p("closed"), "occurrence", "create", "open"),
        (_p("open"), "occurrence", "attach", None),
        (_p("investigating"), "occurrence", "attach", None),
        (_p("waiting_human"), "occurrence", "attach", None),
        (_p("fixing"), "occurrence", "attach", None),
        (_p("verifying"), "occurrence", "attach", None),
        (_p("resolved", timedelta(hours=1)), "occurrence", "reopen", "open"),
        (_p("resolved", timedelta(hours=24)), "occurrence", "reopen", "open"),
        (_p("resolved", timedelta(hours=25)), "occurrence", "rollover", "open"),
        # resolved
        (None, "resolved", "ignore", None),
        (_p("open"), "resolved", "resolve", "resolved"),
        (_p("fixing"), "resolved", "resolve", "resolved"),
        (_p("resolved", timedelta(hours=1)), "resolved", "note", None),
        (_p("closed"), "resolved", "note", None),
        # history kinds never create
        (None, "investigation", "ignore", None),
        (None, "session_note", "ignore", None),
        (_p("open"), "investigation", "note", None),
        (_p("resolved", timedelta(hours=1)), "session_note", "note", None),
        (_p("open"), "plan", "note", None),
    ],
)
def test_decide(current, kind, action, status):
    d = decide(current, kind, now=NOW)
    assert (d.action, d.status) == (action, status)


@pytest.mark.parametrize(
    ("current", "suppressed", "action", "status"),
    [
        # inside a deploy/maintenance window
        (None, True, "create", "suppressed"),
        (_p("open"), True, "attach", None),
        (_p("suppressed"), True, "attach", None),
        (_p("resolved", timedelta(hours=1)), True, "reopen", "suppressed"),
        (_p("resolved", timedelta(hours=48)), True, "rollover", "suppressed"),
        # window over: a suppressed problem that recurs is real
        (_p("suppressed"), False, "promote", "open"),
    ],
)
def test_decide_with_suppression(current, suppressed, action, status):
    d = decide(current, "occurrence", now=NOW, suppressed=suppressed)
    assert (d.action, d.status) == (action, status)


def test_resolved_on_a_suppressed_problem_resolves_it():
    assert decide(_p("suppressed"), "resolved", now=NOW) == decide(_p("open"), "resolved", now=NOW)


def test_reopen_window_is_a_parameter():
    p = _p("resolved", timedelta(hours=2))
    assert decide(p, "occurrence", now=NOW, reopen_window=timedelta(hours=1)).action == "rollover"
    assert decide(p, "occurrence", now=NOW, reopen_window=timedelta(hours=3)).action == "reopen"


def test_naive_resolved_at_is_treated_as_utc():
    p = _p("resolved")
    p["resolved_at"] = (NOW - timedelta(hours=1)).replace(tzinfo=None)
    assert decide(p, "occurrence", now=NOW).action == "reopen"


def _ev(**kw) -> Event:
    base = {"source": "chat", "external_id": "x", "kind": "occurrence", "title": "t"}
    return Event(**{**base, **kw})


@pytest.mark.parametrize(
    "bad",
    [
        {"source": "telegram"},
        {"kind": "explosion"},
        {"external_id": " "},
        {"title": ""},
        {"problem_id": ""},
    ],
)
def test_validate_event_rejects(bad):
    with pytest.raises(ValueError):
        validate_event(_ev(**bad))


def test_validate_event_accepts_a_minimal_event():
    validate_event(_ev())
    validate_event(_ev(problem_id="abc", kind="session_note"))


def test_the_hub_accepts_a_money_finding():
    """`SOURCES` is a closed vocabulary and `validate_event` raises on anything
    outside it, so the money lane cannot report a reconciliation finding at all
    until its source exists (spec 2026-09-07 §15.3).

    Asserted through `validate_event` rather than by reading the frozenset,
    because membership is not the requirement — being able to build a real
    finding is. A `closing_balance` mismatch on one statement is the shape the
    lane will produce: an account-scoped subject, so `reconcile_findings` can
    resolve it by absence when a later statement agrees.
    """
    validate_event(
        Event(
            source="money",
            external_id="stmt/axis-9640/2026-07",
            kind="occurrence",
            title="Closing balance disagrees on axis-9640 2026-07",
            subject="axis-9640",
            subject_kind="instrument",
            klass="closing_balance",
            severity="critical",
        )
    )


def test_a_source_outside_the_vocabulary_is_still_refused():
    """The pin that makes the test above mean something: `validate_event` is a
    real gate, not a no-op that would accept any string."""
    with pytest.raises(ValueError, match="unknown source"):
        validate_event(
            Event(
                source="reconciliation",  # plausible, and not the agreed name
                external_id="x",
                kind="occurrence",
                title="t",
            )
        )
