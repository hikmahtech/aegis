"""Meeting-notes rules — who "you" are in a transcript.

DB-owned so a fork ships nobody's name. Stored in the ``settings`` table under
``meeting_rules``:

    {"self_names": ["Sam Doe", "Sam"]}

``self_names`` is matched case-insensitively against transcript speaker labels,
substring allowed, so "Sam" matches "Sam Doe". Empty means MeetingNotesFlow files
the notes but skips the self-analysis and says so in its result.

``merge`` (read) is lenient and ``validate`` (write) is strict, for the same
reason as ``email_rules.py``: a bad row must never stop notes being filed, but a
typo saved through the admin API must not silently disable every analysis.
Edited at ``GET/PUT /api/admin/email/meeting-rules`` (``routes/email_admin.py``).
"""

from __future__ import annotations

from typing import Any

SETTINGS_KEY = "meeting_rules"

# Deliberately empty: the open-source default carries no name.
DEFAULT_SELF_NAMES: list[str] = []


def merge(value: Any) -> dict:
    """A stored (possibly partial) row merged over the defaults. Never raises.

    A row that is not an object at all reads as empty rather than raising: the
    generic ``PUT /api/settings/meeting_rules`` editor validates nothing, so a
    bare string can reach this table, and a crash here would 500 the admin GET
    and be swallowed into "no names" in the worker."""
    v = value if isinstance(value, dict) else {}
    raw = v.get("self_names")
    names = (
        [str(n).strip() for n in raw if isinstance(n, str) and n.strip()]
        if isinstance(raw, list)
        else []
    )
    return {"self_names": [*DEFAULT_SELF_NAMES, *names]}


def validate(value: Any) -> dict:
    """Strict counterpart to ``merge`` for the WRITE path. Raises ValueError."""
    if value is not None and not isinstance(value, dict):
        raise ValueError("meeting_rules must be an object with self_names")
    v = value or {}
    raw = v.get("self_names")
    if not isinstance(raw, list):
        raise ValueError("self_names must be a list of strings")
    for n in raw:
        if not isinstance(n, str) or not n.strip():
            raise ValueError("self_names entries must be non-empty strings")
    return merge(v)


async def get_meeting_rules(pool: Any) -> dict:
    """Effective rules: DB row (settings.meeting_rules) over the empty defaults."""
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY)
    return merge(row["value"] if row and row["value"] else {})


async def save_meeting_rules(pool: Any, rules: dict) -> dict:
    """Validate then persist. Raises ValueError on bad input."""
    normalised = validate(rules)
    await pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
        SETTINGS_KEY,
        normalised,
    )
    return await get_meeting_rules(pool)


def is_self(speaker: str, self_names: list[str]) -> bool:
    """True when a transcript speaker label is the user."""
    s = (speaker or "").strip().lower()
    if not s:
        return False
    return any(n.strip().lower() in s for n in self_names if isinstance(n, str) and n.strip())
