"""`settings.email_triage_rules` — the user-owned half of email triage.

The open-source default must ship nobody's senders; the personal rules live in
the DB row, edited through the existing generic settings editor.
"""

from __future__ import annotations

import pytest
from aegis.services.email_rules import (
    SETTINGS_KEY,
    get_email_rules,
    match_sender_override,
    merge,
)


def test_defaults_are_empty():
    """A fork must arrive with no opinion about anyone's mailbox."""
    assert merge(None) == {"sender_overrides": {}, "extra_notification_markers": []}


def test_unknown_category_is_dropped_not_raised():
    """One typo'd rule must not take down every classification that reads this."""
    out = merge(
        {"sender_overrides": {"a@b.com": "urgent!!", "c@d.com": "useless"}}
    )
    assert out["sender_overrides"] == {"c@d.com": "useless"}


def test_keys_and_markers_are_lowercased():
    """Both are matched against lowercased input, so they must be stored that way
    or a rule typed with capitals silently never fires."""
    out = merge(
        {
            "sender_overrides": {"  News@Substack.COM ": "informational"},
            "extra_notification_markers": ["  Incorrect Login Attempt  ", "  "],
        }
    )
    assert out["sender_overrides"] == {"news@substack.com": "informational"}
    assert out["extra_notification_markers"] == ["incorrect login attempt"]


def test_exact_address_beats_the_domain_rule():
    overrides = {"@substack.com": "useless", "thegreyswan@substack.com": "important_read"}
    assert match_sender_override(overrides, "thegreyswan@substack.com") == "important_read"
    assert match_sender_override(overrides, "someone@substack.com") == "useless"


def test_domain_key_works_with_or_without_the_at():
    assert match_sender_override({"example.com": "useless"}, "a@example.com") == "useless"
    assert match_sender_override({"@example.com": "useless"}, "a@example.com") == "useless"
    assert match_sender_override({"@example.com": "useless"}, "a@other.com") is None
    assert match_sender_override({"@example.com": "useless"}, "") is None


@pytest.mark.asyncio
async def test_stored_row_merges_over_defaults(db_pool):
    await db_pool.execute(
        "INSERT INTO settings (key, value) VALUES ($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value = $2",
        SETTINGS_KEY,
        {"sender_overrides": {"@substack.com": "informational"}},
    )
    try:
        rules = await get_email_rules(db_pool)
        assert rules["sender_overrides"] == {"@substack.com": "informational"}
        assert rules["extra_notification_markers"] == []
    finally:
        await db_pool.execute("DELETE FROM settings WHERE key = $1", SETTINGS_KEY)
