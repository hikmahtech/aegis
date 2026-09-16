"""Source tag → the agent-task lane's verb, as operator-editable config (#344, #558).

``AgentTaskFlow`` works an agent-assigned Todoist task by the verb its source
tag maps to. The table below is the generic default; a deployment reroutes any
tag with the ``agent_task_verbs`` settings row, merged over it::

    {"#calendar": null, "untagged": "ask"}

``null`` is a decision, not a gap: "nothing here works these, leave them to
me". The worker reads the row through :func:`merge`
(``activities/agent_task.load_verbs``); the admin Todoist page edits it through
:func:`validate` (``GET/PUT /api/admin/todoist/agent-task-verbs``).

EVERY tag AEGIS captures under has an entry in :data:`DEFAULT_VERBS`: a verb,
or an explicit None meaning "decided: nothing here works these". That is the
``_GTD_STATE_FOR`` contract from clarify (#139), and
``tests/worker/activities/test_agent_task_verbs.py`` derives the tag vocabulary
from ``gtd_rules.SOURCE_TAGS`` and the hub's tags, so a new tag added without a
decision fails CI instead of silently parking.

``ask`` hands the task to the agent it is assigned to, through that agent's own
chat path — ``AgentChatReplyFlow``, the executor clarify already uses when you
comment on an agent's task. A ``#chat``, ``#research``, ``#calendar`` or
``#manual`` task given to an agent is a request to that agent; before #344 all
four resolved to no verb, got "No executor for this task type" and parked with
nothing done (prod: an outage question given to the infra agent, an article
given to the research agent).

``research`` (#509) runs ``ResearchFlow`` on a ``#research`` task — knowledge
store, web and papers, a cited answer — and posts the answer on the task.
Under ``ask`` the research agent only chatted about the task; the lane had no
way to actually look anything up.

Read is lenient and write is strict, like every settings row in AEGIS: an entry
naming a verb this lane does not have is ignored on read, so a typo in the row
cannot turn a tag that works into one that parks — but the PUT refuses it, so
the typo is never saved in the first place.
"""

from __future__ import annotations

import re
from typing import Any

from aegis.services.config_rows import SettingsRow

SETTINGS_KEY = "agent_task_verbs"
UNTAGGED = "untagged"  # the key for a task with no source tag

DEFAULT_VERBS: dict[str, str | None] = {
    "#alert": "infra",
    "#receipt": "finance",
    "#email": "email",
    "#chat": "ask",
    "#research": "research",
    "#calendar": "ask",
    "#manual": "ask",
    # A hand-written task carrying an agent's label and no `@code`: somebody
    # gave it to that agent, which is the same request a `#manual` task is.
    UNTAGGED: "ask",
    # Maou raises these and the user acts on them. `EXCLUDED_LABELS` keeps the
    # sweep off them before a verb is ever resolved; this says why.
    "#money": None,
    # A feed that stopped fetching or publishing (#513): the user fixes or
    # drops the feed. Kept off the sweep by `EXCLUDED_LABELS` like `#money`.
    "#feeds": None,
}
# The verbs a tag may be routed to. `coding` is not one: it is chosen by the
# `@code` label on an untagged task, never by a tag.
VERBS = frozenset({"infra", "email", "finance", "ask", "research"})

# A source tag is `#` and a word (the shape every capture writes), or the
# `untagged` key.
_TAG_RE = re.compile(r"^#[A-Za-z0-9_.-]+$")


def merge(value: Any) -> dict[str, str | None]:
    """``DEFAULT_VERBS`` with the stored row merged over it. Never raises.

    An entry that names a verb this lane does not have is ignored, so a typo in
    the row cannot turn a tag that works into one that parks. None is honoured
    — it is how a deployment says "leave these tasks to me".
    """
    merged = dict(DEFAULT_VERBS)
    if not isinstance(value, dict):
        return merged
    for tag, verb in value.items():
        if verb is None or verb in VERBS:
            merged[str(tag)] = verb
    return merged


def overrides_of(value: Any) -> dict[str, str | None]:
    """The stored entries the lenient read would apply (the admin page's rows)."""
    if not isinstance(value, dict):
        return {}
    return {str(t): v for t, v in value.items() if v is None or v in VERBS}


def validate(raw: Any) -> dict[str, str | None]:
    """Normalise the overrides for writing, or raise ValueError (the PUT 400s)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("agent_task_verbs must be an object of source tag → verb")
    out: dict[str, str | None] = {}
    for tag, verb in raw.items():
        name = str(tag).strip()
        if name != UNTAGGED and not _TAG_RE.match(name):
            raise ValueError(
                f"{name!r} is not a source tag — use '#' and a word (e.g. #calendar), "
                f"or {UNTAGGED!r} for a task with no tag"
            )
        if name in out:
            raise ValueError(f"duplicate tag after trimming: {name!r}")
        if verb is not None and verb not in VERBS:
            raise ValueError(
                f"{name}: {verb!r} is not a verb — use one of {', '.join(sorted(VERBS))}, "
                "or null to leave these tasks to you"
            )
        out[name] = verb
    return out


ROW = SettingsRow(SETTINGS_KEY, merge, validate)


async def get_agent_task_verbs(pool: Any) -> dict[str, Any]:
    """What the admin page shows: the overrides, the effective table, the
    defaults under them and the verb vocabulary.

    One :meth:`SettingsRow.raw` read, not two: the page shows the stored
    overrides beside the merged table, and taking them from the same value is
    what keeps the form from showing an override the table below it lacks."""
    value = await ROW.raw(pool)
    return {
        "overrides": overrides_of(value),
        "effective": merge(value),
        "defaults": dict(DEFAULT_VERBS),
        "verbs": sorted(VERBS),
        "untagged": UNTAGGED,
    }


async def save_agent_task_verbs(pool: Any, raw: Any) -> dict[str, Any]:
    """Replace the overrides (validated). ``{}`` deletes the row, so the table
    is the defaults and nothing suggests an override that is not there."""
    stored = validate(raw)
    if stored:
        await ROW.save(pool, stored)
    else:
        await ROW.delete(pool)
    return await get_agent_task_verbs(pool)
