"""Todoist project name → GitHub repo, for the coding lane's tier-1 resolver.

A Todoist work-area project usually mirrors a repository one-to-one, which makes
the project name the strongest signal available for "which checkout is this task
about?" — far stronger than guessing from a task title.

Stored in the ``settings`` table under ``project_repo_map`` as a flat object::

    {"acme app": "Acme/app", "home infra": "acme/infra"}

Ships **EMPTY**, deliberately. This mapping is one operator's project names and
repositories; a fork must not inherit them (issue #345). It previously lived as a
Python constant in ``worker/src/aegis_worker/activities/agent_task.py``, which
meant a public repository shipped one person's Todoist layout and no deployment
could change it without editing code.

Read is lenient and write is strict, mirroring ``email_rules`` and
``content_routes``: a malformed row must never stop a task being resolved (the
resolver just falls through to its later tiers), but that same leniency at the
save boundary would let a typo persist silently forever, so the PUT rejects it.
"""

from __future__ import annotations

import re
from typing import Any

from aegis.services.config_rows import SettingsRow

SETTINGS_KEY = "project_repo_map"

# owner/repo — GitHub's own allowed character set for both segments.
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def validate_map(raw: Any) -> dict[str, str]:
    """Normalize + validate the mapping. Raises ValueError on a bad entry.

    Project names are matched case-insensitively, so they are stored stripped
    and lowercased — the resolver looks up the same way, and without this a
    project named "Home Infra" would never match a key written "home infra".
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("project_repo_map must be an object")
    out: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key or "").strip().lower()
        repo = str(value or "").strip()
        if not name:
            raise ValueError("project name must not be empty")
        if not repo:
            raise ValueError(f"project {name!r}: repo required")
        if not _REPO_RE.match(repo):
            raise ValueError(f"project {name!r}: repo must be 'owner/name', got {repo!r}")
        if name in out:
            raise ValueError(f"duplicate project name after normalising: {name!r}")
        out[name] = repo
    return out


def lookup(project_name: str | None, mapping: dict[str, str]) -> str:
    """The repo for a Todoist project name, or "" — the same normalisation as save."""
    if not project_name:
        return ""
    return mapping.get(str(project_name).strip().lower(), "")


def merge(value: Any) -> dict[str, str]:
    """The lenient READ: the stored mapping, or empty on anything malformed.

    The same normalisation as the write, because there is only one right answer
    to "which repo is this project?" — but a bad entry degrades the resolver to
    its later tiers rather than raising, which is what the write path does."""
    try:
        return validate_map(value if isinstance(value, dict) else {})
    except ValueError:
        return {}


ROW = SettingsRow(SETTINGS_KEY, merge, validate_map)


async def get_project_repo_map(pool: Any) -> dict[str, str]:
    """Effective mapping (validated). Empty on unset or on ANY read/parse error —
    a bad config must degrade the resolver to its later tiers, never break it."""
    return await ROW.get(pool)


async def save_project_repo_map(pool: Any, raw: Any) -> dict[str, str]:
    """Persist the mapping (validated); returns the normalized result."""
    return await ROW.save(pool, raw)
