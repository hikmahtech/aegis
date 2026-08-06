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
    assert out["sender_overrides"] == {"c@d.com": {"category": "useless", "tags": []}}


def test_keys_and_markers_are_lowercased():
    """All three are matched against lowercased input, so they must be stored
    that way or a rule typed with capitals silently never fires."""
    out = merge(
        {
            "sender_overrides": {
                "  News@Substack.COM ": {"category": "informational", "tags": [" Financial "]}
            },
            "extra_notification_markers": ["  Incorrect Login Attempt  ", "  "],
        }
    )
    assert out["sender_overrides"] == {
        "news@substack.com": {"category": "informational", "tags": ["financial"]}
    }
    assert out["extra_notification_markers"] == ["incorrect login attempt"]


def test_bare_string_and_tagged_object_both_normalise():
    """(#263) A rule may carry tags, because an override skips the LLM and the
    money fan-out keys on `financial`/`payments` — without them, silencing a
    biller silently disabled its receipt extraction. Both shapes must survive:
    every rule written before this existed is a bare string."""
    out = merge(
        {
            "sender_overrides": {
                "plain@b.com": "informational",
                "bank@b.com": {"category": "important_read", "tags": ["financial", "receipt"]},
            }
        }
    )
    assert out["sender_overrides"]["plain@b.com"] == {"category": "informational", "tags": []}
    assert out["sender_overrides"]["bank@b.com"] == {
        "category": "important_read",
        "tags": ["financial", "receipt"],
    }


def test_malformed_tags_are_dropped_on_the_read_path():
    """`merge` stays lenient: a junk `tags` value costs the tags, never the
    classification. `validate` is the half that says so out loud."""
    for bad in ("financial", 7, {"financial": True}, None):
        out = merge({"sender_overrides": {"a@b.com": {"category": "useless", "tags": bad}}})
        assert out["sender_overrides"]["a@b.com"] == {"category": "useless", "tags": []}, bad
    # a list with junk entries keeps only the usable ones
    out = merge({"sender_overrides": {"a@b.com": {"category": "useless", "tags": ["ok", 3, "  "]}}})
    assert out["sender_overrides"]["a@b.com"]["tags"] == ["ok"]
    # a dict with no usable category is dropped whole, like a bad bare string
    assert merge({"sender_overrides": {"a@b.com": {"tags": ["financial"]}}})["sender_overrides"] == {}


def test_exact_address_beats_the_domain_rule():
    overrides = merge(
        {
            "sender_overrides": {
                "@substack.com": "useless",
                "thegreyswan@substack.com": "important_read",
            }
        }
    )["sender_overrides"]
    assert match_sender_override(overrides, "thegreyswan@substack.com")["category"] == (
        "important_read"
    )
    assert match_sender_override(overrides, "someone@substack.com")["category"] == "useless"


def test_domain_key_works_with_or_without_the_at():
    bare = {"example.com": {"category": "useless", "tags": []}}
    at = {"@example.com": {"category": "useless", "tags": []}}
    assert match_sender_override(bare, "a@example.com")["category"] == "useless"
    assert match_sender_override(at, "a@example.com")["category"] == "useless"
    assert match_sender_override(at, "a@other.com") is None
    assert match_sender_override(at, "") is None


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
        assert rules["sender_overrides"] == {
            "@substack.com": {"category": "informational", "tags": []}
        }
        assert rules["extra_notification_markers"] == []
    finally:
        await db_pool.execute("DELETE FROM settings WHERE key = $1", SETTINGS_KEY)
