"""Content routing — regex/prefix/contains rules that route Inbox tasks by their
CONTENT (title) to an assignee + labels, optionally behind an ask-before-acting
choice card.

Complements ``gtd_rules`` (which routes by ``source_tag`` — where a task came
from); this routes by what the task's title looks like. Stored in the settings
table under ``content_routes`` as an ordered list; first match wins. Ships
EMPTY — each deployment adds its own routes from the admin UI. The old hardcoded
Acme ``^APP-\\d+:`` → @pandora investigation is now just one such row, e.g.::

    {"key": "jira-app", "match": "prefix", "value": "APP-", "gate": true,
     "assignee": "@pandora", "contexts": ["@code", "@deep"],
     "area_label": "@area/acme", "service": "acme", "resource_tags": ["acme"]}

A route with ``gate: true`` shows the choice card ("investigate / I've got it")
before anything happens; the investigate path spawns AlertInvestigationFlow
scoped by ``service``/``resource_tags``. A route with ``gate: false`` just
applies ``assignee`` + ``contexts`` (+ ``area_label``) directly — plain
label routing, no agent run.
"""

from __future__ import annotations

import re
from typing import Any

SETTINGS_KEY = "content_routes"
MATCH_MODES = ("prefix", "contains", "regex")

# ERE metacharacters. Escaping each with a backslash yields a pattern that means
# the same literal in BOTH Python `re` and Postgres ARE, so one compiled pattern
# drives the worker's `re.search` AND the eligibility SQL's `~ ANY(...)`. (In both
# engines, `\` before a non-alphanumeric matches that character literally.)
_ERE_META = set(r".^$*+?()[]{}|\\")


def _escape_literal(s: str) -> str:
    return "".join("\\" + c if c in _ERE_META else c for c in s)


def compile_pattern(match: str, value: str) -> str:
    """A route's (match, value) → a regex string usable by Python re AND Postgres ~."""
    if match == "prefix":
        return "^" + _escape_literal(value)
    if match == "contains":
        return _escape_literal(value)
    if match == "regex":
        return value
    raise ValueError(f"unknown match mode: {match!r}")


def _valid_pattern(pat: str) -> bool:
    try:
        re.compile(pat)
        return True
    except re.error:
        return False


def validate_routes(routes: Any) -> list[dict]:
    """Normalize + validate a routes list. Raises ValueError on a bad rule so the
    save endpoint can 400; read paths swallow the error and fall back to []."""
    if not isinstance(routes, list):
        raise ValueError("content_routes must be a list")
    out: list[dict] = []
    seen: set[str] = set()
    for i, r in enumerate(routes):
        if not isinstance(r, dict):
            raise ValueError(f"route {i} must be an object")
        key = str(r.get("key") or "").strip()
        match = str(r.get("match") or "").strip()
        value = str(r.get("value") or "")
        if not key:
            raise ValueError(f"route {i}: key required")
        if key in seen:
            raise ValueError(f"duplicate route key: {key!r}")
        seen.add(key)
        if match not in MATCH_MODES:
            raise ValueError(f"route {key!r}: match must be one of {MATCH_MODES}")
        if not value:
            raise ValueError(f"route {key!r}: value required")
        if not _valid_pattern(compile_pattern(match, value)):
            raise ValueError(f"route {key!r}: value is not a valid regex")
        out.append(
            {
                "key": key,
                "match": match,
                "value": value,
                "assignee": str(r.get("assignee") or "@pandora"),
                "contexts": [str(c) for c in (r.get("contexts") or [])],
                "area_label": str(r["area_label"]) if r.get("area_label") else None,
                "gate": bool(r.get("gate", True)),
                "service": str(r["service"]) if r.get("service") else None,
                "resource_tags": [str(t) for t in (r.get("resource_tags") or [])],
            }
        )
    return out


def match_route(content: str, routes: list[dict]) -> dict | None:
    """First route whose compiled pattern matches ``content`` (ordered, first wins)."""
    if not content:
        return None
    for r in routes:
        try:
            if re.search(compile_pattern(r["match"], r["value"]), content):
                return r
        except (re.error, ValueError, KeyError):
            continue
    return None


def active_patterns(routes: list[dict]) -> list[str]:
    """Compiled patterns for the eligibility SQL (``t.content ~ ANY($1)``). Invalid
    patterns are dropped defensively (validate_routes already rejects them on save)."""
    pats: list[str] = []
    for r in routes:
        try:
            pat = compile_pattern(r["match"], r["value"])
        except (ValueError, KeyError):
            continue
        if _valid_pattern(pat):
            pats.append(pat)
    return pats


async def get_content_routes(pool: Any) -> list[dict]:
    """Effective content routes (validated). Empty list when unset or on any read/parse
    error — a bad config must never break classification."""
    try:
        row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY)
        raw = row["value"] if row and row["value"] else []
        return validate_routes(raw)
    except Exception:  # noqa: BLE001 — routing config is best-effort, never fatal
        return []


async def save_content_routes(pool: Any, routes: Any) -> list[dict]:
    """Persist the ordered routes list (validated); returns the normalized result."""
    import asyncpg

    validated = validate_routes(routes)
    # Every pattern must ALSO be valid for Postgres `~` — the eligibility SQL uses
    # `content ~ ANY(...)`. Python `re` and Postgres ARE mostly agree, but not on
    # e.g. lookbehind, so validate here: a bad admin regex must fail the save with
    # a 400, never break the clarify tick's query. Cheap (few patterns, rare save).
    for r in validated:
        pat = compile_pattern(r["match"], r["value"])
        try:
            await pool.fetchval("SELECT ''::text ~ $1", pat)
        except asyncpg.PostgresError as exc:
            raise ValueError(f"route {r['key']!r}: pattern rejected by database: {exc}") from exc
    await pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
        SETTINGS_KEY,
        validated,
    )
    return validated
