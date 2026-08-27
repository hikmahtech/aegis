"""Email → existing-task links.

Email triage only ever *created* Todoist tasks. These rules let an incoming
email change the state of a task AEGIS already tracks — the Jira case being the
obvious one: the ticket is closed, the mail says so, and the Todoist row lives on
forever because nothing was listening.

Stored in ``settings.email_task_links`` as an ordered, first-match-wins list.
Ships EMPTY — each deployment adds its own rules. A rule matches the SUBJECT to
find a task key and (optionally) the BODY for a discriminator, then applies one
action to the open task whose title contains that key::

    {"key": "jira-done",
     "subject_re": "\\\\((APP-\\\\d+)\\\\)",
     "body_re": "resolution\\\\s*:\\\\s*(?:Done|Fixed|Completed|Duplicate|Declined)",
     "action": "complete"}

Group 1 of ``subject_re`` is the task key (the whole match if the pattern has no
group). ``body_re`` is optional but you almost always want one: Jira sends the
same subject for *every* event on an issue, so subject-only matching would close
a ticket because somebody commented on it.

**Write ``body_re`` against a real message, not a guess.** Machine-generated mail
is not prose, and two things that look obviously right are wrong in practice.
Jira's plain-text part renders the field table with no separator between fields,
so a resolution reads ``Resolution : DoneStatus : Deployed`` — a trailing ``\\b``
after ``Done`` never matches, because ``Done`` is glued to ``Status``. And that
table sits ~2400 chars into a body that reaches 15k, well past the classifier's
prompt budget, which is why the flow fetches the whole message for this check
alone (``_LINK_BODY_CHARS``). A rule authored from either assumption matches
nothing and reports nothing, forever.

Actions:

``complete``
    Close the task.
``unblock``
    Drop ``@waiting``, add ``@next`` — the reply you were parked on arrived.
    **Never applied to a task carrying an agent assignee label.** ``@waiting`` is
    overloaded: for a human task it means "blocked on someone", but it is also
    ``agent_task.PARK_LABEL``, stamped at the END of every agent pass precisely
    to drop the task out of ``find_actionable_tasks``. Stripping it there does
    not unblock anything — it re-queues finished agent work, and recreates the
    infinite cooldown loop parking exists to prevent. Measured on real data, 19
    of 25 open ``@waiting`` tasks were agent-parked, so this is the common case,
    not the edge case. See ``UNBLOCK_SKIP_LABELS``.
``comment``
    Leave a note and nothing else.

Reads are lenient: a malformed rule is dropped with a warning rather than
raising, because a typo here must never stop mail being triaged. There is no
dedicated PUT endpoint yet — edit the settings row directly.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from aegis.services.content_routes import compile_pattern

logger = logging.getLogger(__name__)

SETTINGS_KEY = "email_task_links"
ACTIONS = ("complete", "unblock", "comment")

#: Labels the ``unblock`` action swaps.
UNBLOCK_REMOVE = "@waiting"
UNBLOCK_ADD = "@next"

#: An agent assignee label means "an AEGIS agent owns this", and on such a task
#: `@waiting` is `agent_task.PARK_LABEL` — "this pass is done", not "blocked on a
#: human". Removing it re-enters the task into `find_actionable_tasks`, which is
#: the infinite cooldown loop parking exists to prevent. `unblock` therefore
#: refuses these outright; `complete` and `comment` are unaffected, because
#: neither touches the parking state.
#:
#: Kept as a literal rather than resolved from `agents.mention_aliases` on
#: purpose: this is a SAFETY guard, and it must still hold when the DB is
#: unreachable or an agent row has been renamed. A stale extra entry here costs
#: a skipped unblock; a missing one costs re-queued agent work.
UNBLOCK_SKIP_LABELS = ("@sebas", "@raphael", "@maou", "@pandora")


def blocks_unblock(labels: list[str] | None) -> str | None:
    """The agent label that makes `unblock` unsafe on this task, or None."""
    for lab in labels or []:
        if lab in UNBLOCK_SKIP_LABELS:
            return lab
    return None


def merge(raw: Any) -> list[dict]:
    """Normalize a stored rules list, dropping anything malformed.

    Lenient by design (see module docstring). Every returned rule has
    ``key``/``subject_re``/``body_re``/``action`` and a compilable
    ``subject_re``.
    """
    if not isinstance(raw, list):
        if raw:
            logger.warning("email_task_links: expected a list, got %s — ignoring", type(raw))
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for i, r in enumerate(raw):
        if not isinstance(r, dict):
            logger.warning("email_task_links: rule %d is not an object — dropped", i)
            continue
        key = str(r.get("key") or "").strip()
        subject_re = str(r.get("subject_re") or "")
        body_re = str(r.get("body_re") or "")
        action = str(r.get("action") or "").strip()
        if not key or key in seen:
            logger.warning("email_task_links: rule %d has a missing/duplicate key — dropped", i)
            continue
        if action not in ACTIONS:
            logger.warning(
                "email_task_links: rule %r action %r not one of %s — dropped", key, action, ACTIONS
            )
            continue
        if not _compiles(subject_re) or (body_re and not _compiles(body_re)):
            logger.warning("email_task_links: rule %r has an invalid regex — dropped", key)
            continue
        seen.add(key)
        out.append(
            {"key": key, "subject_re": subject_re, "body_re": body_re or None, "action": action}
        )
    return out


def _compiles(pattern: str) -> bool:
    if not pattern:
        return False
    try:
        re.compile(pattern)
        return True
    except re.error:
        return False


def match_link(links: list[dict], subject: str, body: str) -> dict | None:
    """First rule matching ``subject`` (and ``body``, when it sets ``body_re``).

    Returns ``{"key", "action", "task_key"}`` where ``task_key`` is the string to
    look for in the Todoist task title, or None.
    """
    if not subject:
        return None
    for r in links:
        m = re.search(r["subject_re"], subject)
        if not m:
            continue
        if r["body_re"] and not re.search(r["body_re"], body or "", re.I | re.S):
            continue
        task_key = (m.group(1) if m.groups() else m.group(0)).strip()
        if not task_key:
            continue
        return {"key": r["key"], "action": r["action"], "task_key": task_key}
    return None


def task_key_pattern(task_key: str) -> str:
    """A Postgres-ARE pattern matching ``task_key`` as a whole word in a title.

    Word-bounded so ``APP-12`` never matches ``APP-123``, and literal-escaped so a
    key carrying regex metacharacters can't turn into a wildcard. Placement-agnostic
    — Jira→Todoist syncs write both ``APP-1: Title`` and ``Title (APP-1)``.
    """
    return r"\m" + compile_pattern("contains", task_key) + r"\M"


async def get_email_task_links(pool: Any) -> list[dict]:
    """Effective rules. Empty list when unset or on any read error."""
    try:
        row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY)
    except Exception as exc:
        logger.warning("email_task_links: read failed (%s) — no rules applied", str(exc)[:200])
        return []
    if not row or not row["value"]:
        return []
    value = row["value"]
    if isinstance(value, str):
        import json

        try:
            value = json.loads(value)
        except ValueError:
            logger.warning("email_task_links: stored value is not JSON — no rules applied")
            return []
    if isinstance(value, dict):
        value = value.get("value")
    return merge(value)
