"""C8 — source_type registry.

Two things this locks in:

1. Every `source_type` literal verified (by grep, not by trusting the
   improvement-observations.md doc) across core/worker/comms is present in
   `SOURCE_TYPES` — including "document" (comms/slack_inbound.py), which the
   doc's audit list missed.
2. `get_decay_days` reproduces exactly what the OLD chat.py `DECAY_WINDOWS`
   dict + `DEFAULT_DECAY_WINDOW` produced, for every previously-configured
   type plus the unknown-type fallback. The expected values below were
   captured from chat.py BEFORE the migration:
       DECAY_WINDOWS = {"chat": 30, "task_outcome": 60, "triage": 90,
                         "content": 180, "manual": 365}
       DEFAULT_DECAY_WINDOW = 90
"""

from __future__ import annotations

import structlog.testing
from aegis.services.source_types import SOURCE_TYPES, get_decay_days, warn_if_unknown

# Every literal source_type actually written/read against knowledge_content,
# confirmed by grep across core/src, worker/src, comms/src (not copied blind
# from the doc's enumeration — see the PR description for the "document"
# discrepancy this caught).
VERIFIED_LITERALS = {
    # improvement-observations.md's own list
    "calendar",  # services/claims.py
    "drive",  # services/drive.py, drive.py (worker), drive_sync.py, schedule_sync.py
    "chat",  # chat.py remember_this
    "research",  # chat.py research tool
    "runbook",  # chat.py runbook tool
    "reference",  # chat.py search filter; clarify.py writes
    "content",  # knowledge.py route default; content.py ingest_content default
    "upload",  # knowledge.py upload/folder-seed routes
    "media",  # worker content.py _transcribe_media
    "email",  # worker gmail.py
    "intelligence",  # worker briefing.py, intelligence.py
    "briefing",  # worker briefing.py
    "alert",  # worker alerts.py
    "alert_investigation",  # worker alerts.py
    # decay-only entries (pre-configured in the old DECAY_WINDOWS dict but
    # with no current write call site — kept as reserved/planned)
    "task_outcome",
    "triage",
    "manual",
    # content-type-derived literals from content.py's detect_content_type()
    # (the doc bucketed these as "content-type-derived" without naming them)
    "article",
    "pdf",
    "image",
    # NOT in the doc's audit list — found by grep in comms/slack_inbound.py's
    # SlackCoreClient.knowledge_ingest (POST /api/knowledge/ingest)
    "document",
    # life-domain types pre-registered for upcoming Themes A/B/C
    "people",
    "observation",
    "expiring_item",
    "asset",
    "life_fact",
    "daylog",
    "daylog_rollup",
    # MeetingNotesFlow (worker activities/meeting.py, flows/meeting_notes.py)
    "meeting",
    "meeting_review",
}


def test_all_verified_literals_are_registered():
    missing = VERIFIED_LITERALS - set(SOURCE_TYPES)
    assert not missing, f"literals missing from SOURCE_TYPES: {sorted(missing)}"


def test_every_registry_entry_has_a_description():
    for source_type, info in SOURCE_TYPES.items():
        assert info.description, f"{source_type} has no description"


def test_warn_if_unknown_logs_for_made_up_type():
    with structlog.testing.capture_logs() as log_entries:
        warn_if_unknown("definitely-not-a-real-source-type")
    assert any(
        e.get("event") == "unknown_source_type"
        and e.get("source_type") == "definitely-not-a-real-source-type"
        for e in log_entries
    )


def test_warn_if_unknown_silent_for_known_type():
    with structlog.testing.capture_logs() as log_entries:
        warn_if_unknown("chat")
    assert log_entries == []


# --- decay regression (behavior must be unchanged after the DECAY_WINDOWS ->
# source_types.py migration) ---

_OLD_DECAY_WINDOWS = {
    "chat": 30,
    "task_outcome": 60,
    "triage": 90,
    "content": 180,
    "manual": 365,
}
_OLD_DEFAULT_DECAY_WINDOW = 90


def test_decay_days_matches_old_decay_windows_dict_for_configured_types():
    for source_type, expected_days in _OLD_DECAY_WINDOWS.items():
        assert get_decay_days(source_type) == expected_days


def test_decay_days_matches_old_default_for_unknown_type():
    assert get_decay_days("some-type-not-in-the-old-dict") == _OLD_DEFAULT_DECAY_WINDOW
