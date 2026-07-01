"""GTD clarify rules — the source-tag → assignee / contexts / skip-inbox taxonomy,
editable from the admin UI (was hardcoded in worker clarify.py::_RuleSet).

Lives in core (a worker dependency) so both the worker's classifier and the core
admin route share the defaults + merge. Stored in the settings table under
``gtd_rules`` as ``{assignee, contexts, skip_inbox}``; the worker resolves it
DB-first (merged over these defaults). The @sebas/@raphael/@maou/@pandora
*addressable* routing stays hardcoded in clarify.py — it's behavioural, not data.
"""

from __future__ import annotations

from typing import Any

# Source tags the clarify pipeline recognises (the UI edits rules for these).
SOURCE_TAGS = ["#email", "#alert", "#receipt", "#research", "#calendar", "#manual", "#chat"]

DEFAULT_ASSIGNEE: dict[str, str] = {
    "#email": "@sebas",
    "#alert": "@pandora",
    "#receipt": "@maou",
    "#research": "@raphael",
    "#calendar": "@sebas",
    "#manual": "@me",
    "#chat": "@me",
}
DEFAULT_CONTEXTS: dict[str, list[str]] = {
    "#email": ["@email", "@5min"],
    "#alert": ["@code", "@deep"],
    "#receipt": ["@deep"],
    "#research": ["@reading"],
    "#calendar": ["@deep"],
    "#manual": ["@deep"],
    "#chat": ["@deep"],
}
DEFAULT_SKIP_INBOX: dict[str, str] = {"#research": "reference"}

SETTINGS_KEY = "gtd_rules"


def merge(value: dict | None) -> dict:
    """A stored (possibly partial) override merged over the defaults."""
    v = value or {}
    return {
        "assignee": {**DEFAULT_ASSIGNEE, **(v.get("assignee") or {})},
        "contexts": {**DEFAULT_CONTEXTS, **(v.get("contexts") or {})},
        "skip_inbox": {**DEFAULT_SKIP_INBOX, **(v.get("skip_inbox") or {})},
    }


async def get_gtd_rules(pool: Any) -> dict:
    """The effective rules: DB override (settings.gtd_rules) merged over defaults."""
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY)
    return merge(row["value"] if row and row["value"] else {})


async def save_gtd_rules(pool: Any, rules: dict) -> dict:
    """Persist the assignee/contexts/skip_inbox maps; returns the merged result."""
    stored = {k: rules[k] for k in ("assignee", "contexts", "skip_inbox") if k in rules}
    await pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
        SETTINGS_KEY,
        stored,
    )
    return await get_gtd_rules(pool)
