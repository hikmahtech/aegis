"""Email triage rules — your personal sender verdicts and notification markers.

Everything here is DB-owned so a fork ships nobody's mailbox. Stored in the
``settings`` table under ``email_triage_rules`` and merged over the (empty)
defaults below; edit it from the admin Settings page or
``PUT /api/settings/email_triage_rules`` — the generic key/value editor already
covers it, so there is deliberately no dedicated endpoint or UI page.

    {
      "sender_overrides": {
        "@substack.com": "informational",
        "no-reply@accounts.google.com": "important_read",
        "tax@jbpassociates.in": "important_action"
      },
      "extra_notification_markers": ["incorrect login attempt", "account unlocked"]
    }

``sender_overrides`` is checked FIRST in ``classify_email`` — ahead of the
per-sender cache and the LLM — and deliberately does not write ``triage_state``:
an override must stop applying the moment you delete it, not live on as learned
sender state. ``extra_notification_markers`` extend the shared
``_NOTIFICATION_MARKERS`` list that caps courtesy notifications out of
``important_action``.

Lives in core (a worker dependency) so the worker classifier and the admin API
share one definition, matching ``services/gtd_rules.py``.
"""

from __future__ import annotations

from typing import Any

SETTINGS_KEY = "email_triage_rules"

CATEGORIES = ("important_action", "important_read", "informational", "useless")

# Both deliberately empty: the open-source default must carry no personal
# senders and no mailbox-specific phrasing. Yours live in the DB row.
DEFAULT_SENDER_OVERRIDES: dict[str, str] = {}
DEFAULT_EXTRA_NOTIFICATION_MARKERS: list[str] = []


def merge(value: dict | None) -> dict:
    """A stored (possibly partial) override merged over the defaults.

    Unknown categories are dropped rather than raising — a typo in one sender
    entry must not take the whole ruleset (and with it every classification)
    down. Keys and markers are normalised to lowercase because both are matched
    against lowercased input.
    """
    v = value or {}
    overrides = {
        str(addr).strip().lower(): str(cat)
        for addr, cat in (v.get("sender_overrides") or {}).items()
        if str(cat) in CATEGORIES and str(addr).strip()
    }
    markers = [
        str(m).strip().lower() for m in (v.get("extra_notification_markers") or []) if str(m).strip()
    ]
    return {
        "sender_overrides": {**DEFAULT_SENDER_OVERRIDES, **overrides},
        "extra_notification_markers": [*DEFAULT_EXTRA_NOTIFICATION_MARKERS, *markers],
    }


async def get_email_rules(pool: Any) -> dict:
    """The effective rules: DB override (settings.email_triage_rules) over defaults."""
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY)
    return merge(row["value"] if row and row["value"] else {})


def match_sender_override(overrides: dict[str, str], sender: str) -> str | None:
    """Exact address wins over the domain, so one sender can be pulled out of a
    blanket domain rule. `sender` must already be a bare lowercased address
    (``_normalize_sender``). Returns None when nothing matches.
    """
    addr = (sender or "").strip().lower()
    if not addr:
        return None
    if addr in overrides:
        return overrides[addr]
    domain = addr.rpartition("@")[2]
    if not domain:
        return None
    # Accept both "@example.com" and "example.com" as domain keys — the leading
    # @ reads better in the settings JSON and is the shape people type.
    return overrides.get(f"@{domain}") or overrides.get(domain)
