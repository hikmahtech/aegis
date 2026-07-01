"""Test CalendarIngestFlow action-language predicate (Phase 2)."""

from __future__ import annotations

from aegis_worker.flows.calendar_ingest import _ACTION_LANGUAGE_RE


def test_action_language_matches_rsvp():
    assert _ACTION_LANGUAGE_RE.search("Please RSVP by Friday") is not None


def test_action_language_matches_due():
    assert _ACTION_LANGUAGE_RE.search("Report due tomorrow") is not None


def test_action_language_case_insensitive():
    assert _ACTION_LANGUAGE_RE.search("Submit feedback") is not None
    assert _ACTION_LANGUAGE_RE.search("SUBMIT feedback") is not None


def test_action_language_no_match_for_neutral_event():
    assert _ACTION_LANGUAGE_RE.search("Team standup") is None
    assert _ACTION_LANGUAGE_RE.search("Lunch with Alice") is None


def test_action_language_matches_deadline():
    assert _ACTION_LANGUAGE_RE.search("Project deadline approaching") is not None


def test_action_language_matches_register():
    assert _ACTION_LANGUAGE_RE.search("Register for the conference") is not None


def test_action_language_matches_confirm():
    assert _ACTION_LANGUAGE_RE.search("Please confirm your attendance") is not None


def test_action_language_matches_prepare():
    assert _ACTION_LANGUAGE_RE.search("Prepare slides for the meeting") is not None


def test_action_language_word_boundary():
    """Regex uses \b — partial matches inside longer words should not fire."""
    # 'due' inside 'education' should not match
    assert _ACTION_LANGUAGE_RE.search("Education session") is None
