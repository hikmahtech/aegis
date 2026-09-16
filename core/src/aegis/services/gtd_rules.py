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

from aegis.services.config_rows import SettingsRow

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


# The three maps this row holds. `source_tags` is what the GET adds on top;
# it is accepted and ignored on the PUT so the page can send back what it read.
_WRITABLE_KEYS = ("assignee", "contexts", "skip_inbox")
_COMPUTED_KEYS = frozenset({"source_tags"})


def _tag_map(v: dict, key: str) -> dict[str, str]:
    """`v[key]` as a tag → label map, or raise. Absent is an empty map."""
    raw = v.get(key)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{key} must be an object of source tag → label")
    out: dict[str, str] = {}
    for tag, label in raw.items():
        name = str(tag).strip()
        if not name:
            raise ValueError(f"{key} has an entry with an empty source tag")
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"{key}[{name}]: {label!r} is not a label")
        out[name] = label.strip()
    return out


def validate(value: Any) -> dict:
    """Strict counterpart to :func:`merge`, for the WRITE path only.

    ``merge`` is lenient because a half-written row must never stop clarify
    routing a task. That is wrong at the save boundary: a mistyped key would
    save with a 200 and then silently do nothing, which is the failure every
    settings row in AEGIS is written to avoid. Raises ValueError; the route
    turns it into a 400.
    """
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("gtd_rules must be an object with assignee, contexts and skip_inbox")
    unknown = sorted(set(value) - set(_WRITABLE_KEYS) - _COMPUTED_KEYS)
    if unknown:
        raise ValueError(f"unknown key(s): {', '.join(unknown)}; allowed: {', '.join(_WRITABLE_KEYS)}")
    contexts_raw = value.get("contexts")
    if contexts_raw is not None and not isinstance(contexts_raw, dict):
        raise ValueError("contexts must be an object of source tag → list of context labels")
    contexts: dict[str, list[str]] = {}
    for tag, labels in (contexts_raw or {}).items():
        name = str(tag).strip()
        if not name:
            raise ValueError("contexts has an entry with an empty source tag")
        if not isinstance(labels, list) or not all(
            isinstance(c, str) and c.strip() for c in labels
        ):
            raise ValueError(f"contexts[{name}] must be a list of non-empty context labels")
        contexts[name] = [c.strip() for c in labels]
    return {
        "assignee": _tag_map(value, "assignee"),
        "contexts": contexts,
        "skip_inbox": _tag_map(value, "skip_inbox"),
    }


ROW = SettingsRow(SETTINGS_KEY, merge, validate)


async def get_gtd_rules(pool: Any) -> dict:
    """The effective rules: DB override (settings.gtd_rules) merged over defaults."""
    return await ROW.get(pool)


async def save_gtd_rules(pool: Any, rules: dict) -> dict:
    """Persist the assignee/contexts/skip_inbox maps; returns the merged result.
    Raises ValueError on a malformed map (the route answers 400)."""
    return await ROW.save(pool, rules)
