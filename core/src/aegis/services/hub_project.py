"""Projection of a problem onto its human surface: the Todoist task.

Spec: docs/superpowers/specs/2026-09-07-problem-hub-design.md §6.

The problem (`services/hub.py`) is the record; the task is a *view* of it.
The status block in the task description is never read back — it is for the
human and for a session that has only the task in front of it. Two things
flow from Todoist into AEGIS: a user's own comment, over the webhook that
already exists, and a task somebody completed, which
:func:`reconcile_completed_tasks` turns into a resolve — or, when the
completion turns out to be the hub's own close from before a return, undoes
(#473).

What a projection does, in order:

1. **Ensures the task** once the problem is worth a human's attention: any
   projected status (§4) short of `resolved`, not suppressed, not muted. A
   problem that is already over when it is first projected gets no task.
   Created through the same idempotent capture the chat tools use, with
   `external_id = problem-<id>`, so a retried projection finds its own task.
2. **Comments, never new tasks.** Every event since the last projection
   becomes a comment — except occurrences, which are collapsed to one
   "N more" comment per ``COLLAPSE_WINDOW`` so a flapping service cannot flood
   the thread, and resolves and reopens, which are told once, as where the
   batch ends. A `resolve` closes the task (unless the user has claimed it
   with `@me`); a `reopen` or `promote` reopens it. A mute silences
   occurrences and returns, never a recovery: a muted problem that resolves
   still closes its task.
3. **Re-renders the status block** in the description, replaced whole between
   its markers, only when its content changed.
4. **Turns a plan into subtasks.** A `plan` event carrying two or more
   ``steps`` creates one Todoist subtask per step under the problem's task,
   linked as ``plan_step`` so the same plan never creates them twice; a later
   event carrying ``step_done`` completes that step's subtask. One step is not
   a plan, so it stays a comment.

Comments carry the ``Workflow run:`` token so `is_user_note` and clarify keep
excluding them, and a comment that fails to post leaves the watermark where it
was: the next sweep retries rather than the event being lost.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import structlog

from aegis.connectors.todoist import TodoistConnector
from aegis.services import work_sessions
from aegis.services.agents import resolve_tag
from aegis.services.books import parse_kv
from aegis.services.hub import (
    LIVE_STATUSES,
    QUESTION_CLASS,
    TASK_SUBJECT_KIND,
    TOPIC_CLASS,
    _active_suppression,
    _aware,
    get_problem,
    set_status,
)
from aegis.services.todoist_config import resolve_todoist_api_key
from aegis.services.tools.gtd import _capture_to_inbox_impl

logger = structlog.get_logger()

# The literal token clarify's loop guard and `work_sessions.is_user_note` key on.
FOOTER = "\n\nWorkflow run: problem-hub"
SOURCE_TAG = "#alert"
# The tag on a money problem's task (see `_OWNER_BY_SOURCE`). Clarify reads it
# too: such a task is the user's to act on, never the classifier's.
MONEY_SOURCE_TAG = "#money"
COLLAPSE_WINDOW = timedelta(minutes=30)
# Statuses that earn a task. `suppressed` and `closed` never do.
PROJECTED_STATUSES = frozenset(
    {"open", "investigating", "waiting_human", "fixing", "verifying", "resolved"}
)
_BLOCK_RE = re.compile(r"<!-- aegis:problem [^>]*-->.*?<!-- /aegis:problem -->", re.S)
_HISTORY_KINDS = frozenset({"investigation", "plan", "session_note"})
# A plan of one step is a sentence, not a plan; more than this and the
# subtask list is noise rather than a checklist.
_MIN_PLAN_STEPS = 2
_MAX_PLAN_STEPS = 12
_STEP_CAP = 200
_FALLBACK_LABEL = "@pandora"
_DESCRIPTION_CAP = 2000
# What the timeline says when a completed task resolved its problem.
TASK_COMPLETED_REASON = "its Todoist task was completed by a person, not by the hub"
# The `source` that resolve is written with, which starts its `state_change`
# external id `todoist:`. No ingested event can start that way — `todoist` is
# not in `hub.SOURCES` — so the prefix tells a person's completion apart from
# every other resolve without matching on the reason's wording.
TASK_COMPLETED_SOURCE = "todoist"
# Where the admin Integrations page stores `books_todoist_projects`
# (`integrations_config`, prefix `integration:`).
_BOOKS_PROJECTS_SETTING = "integration:books_todoist_projects"


@dataclass(frozen=True)
class _Owner:
    """Who a problem's task belongs to, and so how it is tagged and filed."""

    source_tag: str
    agent_tag: str
    fallback_label: str
    # Labels after the assignee's.
    extra_labels: tuple[str, ...] = ()
    # The `books_todoist_projects` entry the task is filed in; "" is the Inbox.
    books_entity: str = ""


_INFRA_OWNER = _Owner(SOURCE_TAG, "infra", _FALLBACK_LABEL)
# Problems another agent owns, by the source of their first occurrence. All 13
# money problems in prod (2026-09-11) were projected as `#alert @pandora` in the
# Inbox, and the agent sweep then ran Pandora's infra verb on them and parked
# them. Money problems are Maou's: Maou raises them and the user acts.
#
# * `#money` comes first because the Todoist mirror takes the first `#` label
#   as the task's source tag (`activities/todoist.py::_pick_source_tag`).
# * `@next`, because the task lives outside the Inbox, so clarify never gives it
#   a GTD state label, and every task needs one (#139).
#
# The key is spelled here, not imported: `statement_findings.SOURCE` is the
# same word, but that module imports this one.
#
# The research agent's two kinds (#513), both in the Inbox, both `@next`:
#
# * `#research`: a tracked topic's round that crossed its threshold, or a
#   `#research` task's question. Raphael can work these, so the agent sweep
#   may run the `research` verb on them.
# * `#feeds`: a feed that stopped fetching or publishing. The user fixes or
#   drops it; `agent_task.EXCLUDED_LABELS` keeps the sweep off it.
#
# Clarify treats both as hub-owned (`activities/clarify.py`), or its
# `#research → reference` rule would file a topic task as a reference and
# complete it.
RESEARCH_SOURCE_TAG = "#research"
FEEDS_SOURCE_TAG = "#feeds"
_OWNER_BY_SOURCE = {
    "money": _Owner(MONEY_SOURCE_TAG, "finance", "@maou", ("@next",), "personal"),
    "research": _Owner(RESEARCH_SOURCE_TAG, "research", "@raphael", ("@next",)),
    "feeds": _Owner(FEEDS_SOURCE_TAG, "research", "@raphael", ("@next",)),
}
# Every tag a projected task can carry. The agent lane needs a verb decision
# for each (`test_agent_task_verbs`).
HUB_SOURCE_TAGS = frozenset({SOURCE_TAG, *(o.source_tag for o in _OWNER_BY_SOURCE.values())})


def _ts(value: Any) -> str:
    if isinstance(value, datetime):
        return f"{_aware(value, value):%Y-%m-%d %H:%M} UTC"
    return str(value or "")


def render_block(
    problem: dict[str, Any],
    *,
    window: dict[str, Any] | None = None,
    links: list[dict[str, Any]] | None = None,
    sessions: list[dict[str, Any]] | None = None,
    steps: str = "",
) -> str:
    """The status block for a task description. Pure."""
    lines = [
        f"<!-- aegis:problem {problem['id']} -->",
        f"Status: {problem['status']} · seen {problem['occurrences']}× since "
        f"{_ts(problem['first_seen_at'])} · last {_ts(problem['last_seen_at'])}",
        f"Subject: {problem['subject'] or '-'} ({problem['subject_kind'] or '-'}) · "
        f"{problem['severity']} · class {problem['class']}",
    ]
    if problem.get("group_key"):
        # A group stands for a whole class, so its subject is `*`. Say what it
        # covers instead, and that the next one joins it rather than opening
        # another task.
        lines.append(
            f"Group: every {problem['class']} on a {problem['subject_kind'] or 'subject'} · "
            "new ones join this problem"
        )
    if window:
        until = f"until {_ts(window['until_at'])}" if window.get("until_at") else "until cleared"
        lines.append(f"Window: {window['state']} {until} (set by {window['set_by']})")
    refs = [
        f"{link['link_kind']}:{link['ref']}"
        for link in (links or [])
        if link["link_kind"] != "todoist_task"
    ]
    if refs:
        lines.append("Links: " + " · ".join(refs))
    if steps:
        lines.append("Steps: " + steps)
    for sess in sessions or []:
        lines.append("Session: " + session_line(sess))
    lines.append("<!-- /aegis:problem -->")
    return "\n".join(lines)


def session_line(sess: dict[str, Any]) -> str:
    """One registry row as the block and `task_context` show it:
    `aegis active (work) · seen 2026-09-07 10:12 UTC · <summary>`."""
    who = str(sess.get("owner") or "aegis")
    account = str(sess.get("account") or "")
    head = f"{who} {sess.get('status') or 'active'}" + (f" ({account})" if account else "")
    seen = sess.get("last_seen_at") or sess.get("last_turn_at")
    if seen:
        head += f" · seen {_ts(seen)}"
    summary = str(sess.get("summary") or "").strip()
    return f"{head} · {summary[:160]}" if summary else head


def plan_steps(payload: dict[str, Any]) -> list[str]:
    """The step list on a `plan` payload, cleaned and bounded. `[]` when the
    payload carries no plan worth a checklist."""
    raw = payload.get("steps")
    if not isinstance(raw, list):
        return []
    steps = [str(s).strip()[:_STEP_CAP] for s in raw if str(s).strip()]
    return steps[:_MAX_PLAN_STEPS] if len(steps) >= _MIN_PLAN_STEPS else []


async def _step_links(pool: asyncpg.Pool, problem_id: str) -> list[dict[str, Any]]:
    """The problem's plan steps, in order: ``[{index, task_id}]``. The ref is
    ``<index>:<subtask id>`` so the order survives without a column."""
    rows = await pool.fetch(
        "SELECT ref FROM problem_links WHERE problem_id = $1::uuid AND link_kind = 'plan_step'",
        problem_id,
    )
    out = []
    for r in rows:
        index, _, task_id = str(r["ref"]).partition(":")
        if index.isdigit() and task_id:
            out.append({"index": int(index), "task_id": task_id})
    return sorted(out, key=lambda s: s["index"])


async def _create_plan_steps(
    pool: asyncpg.Pool, settings: Any, problem_id: str, task_id: str, steps: list[str]
) -> int:
    """One subtask per step, linked as `plan_step`. Idempotent: a problem that
    already has steps keeps them, because a re-plan that recreated them would
    orphan whatever the session has already ticked off.

    Created through the connector rather than the outbox: the subtask's real id
    is what the link stores, and an outbox temp id would leave the step
    unclosable until the sync drained.
    """
    if not steps or await _step_links(pool, problem_id):
        return 0
    try:
        key = await resolve_todoist_api_key(pool, settings or _settings())
        if not key:
            return 0
        connector = TodoistConnector(api_key=key, db_pool=pool, timeout=10.0)
        cmds = [TodoistConnector.build_subtask_add_command(task_id, s) for s in steps]
        try:
            result = await connector.commands(cmds)
        finally:
            await connector.close()
        status = TodoistConnector.check_sync_status(result, [c["uuid"] for c in cmds])
        if not status["ok"]:
            logger.warning("hub_project_steps_failed", task_id=task_id, status=status)
            return 0
        mapping = ((result or {}).get("data") or {}).get("temp_id_mapping") or {}
    except Exception as exc:  # noqa: BLE001 — a plan is worth a comment even with no subtasks
        logger.warning("hub_project_steps_failed", task_id=task_id, error=str(exc)[:200])
        return 0
    made = 0
    for index, cmd in enumerate(cmds, start=1):
        real = str(mapping.get(cmd["temp_id"]) or "")
        if not real:
            continue
        await pool.execute(
            "INSERT INTO problem_links (problem_id, link_kind, ref) "
            "VALUES ($1::uuid, 'plan_step', $2) ON CONFLICT DO NOTHING",
            problem_id,
            f"{index}:{real}",
        )
        made += 1
    logger.info("hub_project_steps_created", problem_id=problem_id, steps=made)
    return made


async def _complete_step(pool: asyncpg.Pool, problem_id: str, index: int) -> bool:
    """Tick off step `index` (1-based). False when there is no such step."""
    step = next((s for s in await _step_links(pool, problem_id) if s["index"] == index), None)
    if step is None:
        return False
    await _queue(
        pool,
        f"problem-step-{step['task_id']}",
        TodoistConnector.build_item_complete_command(step["task_id"]),
    )
    await pool.execute(
        "UPDATE todoist_tasks SET is_completed = true, updated_at = now() WHERE id = $1",
        step["task_id"],
    )
    return True


async def _step_progress(pool: asyncpg.Pool, problem_id: str) -> str:
    """`"2/5 done"` for the status block, or `""` when there is no plan."""
    steps = await _step_links(pool, problem_id)
    if not steps:
        return ""
    done = await pool.fetchval(
        "SELECT count(*) FROM todoist_tasks WHERE id = ANY($1::text[]) AND is_completed",
        [s["task_id"] for s in steps],
    )
    return f"{int(done or 0)}/{len(steps)} done"


async def ensure_problem_for_task(
    pool: asyncpg.Pool,
    task_id: str,
    *,
    source: str = "session",
    subject: str = "",
    settings: Any = None,
) -> dict[str, Any] | None:
    """The problem behind a Todoist task, creating a `manual` one when the task
    has none. This is what lets the hub carry a plain `@code` task: a session
    registry, a timeline and a plan need a problem to hang off, and a task the
    user wrote by hand has no alert behind it.

    Returns the problem, or None when the task is unknown or the hub refused
    the event.
    """
    from aegis.services.hub import Event, find_problem_for_task, get_problem, ingest_event

    problem = await find_problem_for_task(pool, task_id)
    # A closed problem is history: its projection is over, so a note attached
    # to it would land nowhere. The task gets a fresh one instead.
    if problem is not None and problem["closed_at"] is None:
        return problem
    row = await pool.fetchrow("SELECT content FROM todoist_tasks WHERE id = $1", task_id)
    if row is None:
        return None
    # The event id is what makes two callers racing for one task idempotent,
    # so it stays derived from the task — but the CLOSED problem's id is part
    # of it, or the claim made by the first problem would answer `duplicate`
    # for ever and the task could never have a second one.
    external_id = f"task-{task_id}" + (f"@{problem['id']}" if problem is not None else "")
    try:
        result = await ingest_event(
            pool,
            Event(
                source=source,
                external_id=external_id,
                kind="occurrence",
                title=str(row["content"] or f"Task {task_id}")[:200],
                # The TASK is the subject, never the repo. Keying a manual
                # problem on the repo made every task in one repo the same
                # problem: the second task's session notes, PR links and
                # comments all landed on the first task. The repo is context,
                # so it rides in the payload.
                subject=f"task-{task_id}",
                # A `#research` task (#513) is a question, Raphael's: class
                # `question`, kind `task` — a kind the hub never groups. The
                # `@code` path keeps `manual` on the repo kind, as before.
                subject_kind=TASK_SUBJECT_KIND if source == "research" else "repo",
                klass=QUESTION_CLASS if source == "research" else "manual",
                severity="info",
                payload={"task_id": task_id, "github_repo": subject},
            ),
        )
    except ValueError as exc:
        logger.warning("hub_problem_for_task_refused", task_id=task_id, error=str(exc)[:200])
        return None
    if not result.problem_id:
        return None
    await link_task(pool, result.problem_id, task_id)
    return await get_problem(pool, result.problem_id)


def merge_block(description: str | None, block: str) -> str:
    """Replace the existing block in ``description`` or append one. The user's
    text around it is never touched."""
    base = description or ""
    if _BLOCK_RE.search(base):
        return _BLOCK_RE.sub(lambda _m: block, base, count=1)
    return (base.rstrip() + "\n\n" + block) if base.strip() else block


async def _assignee_label(
    pool: asyncpg.Pool, tag: str = "infra", fallback: str = _FALLBACK_LABEL
) -> str:
    """The label that assigns the task to the agent holding ``tag`` — its first
    mention alias — falling back to ``fallback``. Never raises."""
    try:
        agent_id = await resolve_tag(pool, tag)
        if not agent_id:
            return fallback
        meta = await pool.fetchval("SELECT metadata FROM agents WHERE id = $1", agent_id)
        aliases = (meta or {}).get("mention_aliases") or [agent_id]
        return f"@{str(aliases[0]).lstrip('@')}"
    except Exception as exc:  # noqa: BLE001 — a label lookup must never block a task
        logger.warning("hub_project_label_failed", tag=tag, error=str(exc)[:200])
        return fallback


async def _owner(pool: asyncpg.Pool, problem_id: str) -> _Owner:
    """Who owns the problem: decided by the source of its FIRST occurrence, the
    producer that raised it."""
    source = await pool.fetchval(
        "SELECT source FROM problem_events "
        "WHERE problem_id = $1::uuid AND kind = 'occurrence' ORDER BY id LIMIT 1",
        problem_id,
    )
    return _OWNER_BY_SOURCE.get(str(source or ""), _INFRA_OWNER)


async def _books_project(pool: asyncpg.Pool, entity: str) -> str | None:
    """The Todoist project `books_todoist_projects` names for ``entity``: the DB
    row first, then the env, then None — the Inbox. Never raises."""
    raw = ""
    try:
        stored = await pool.fetchval(
            "SELECT value FROM settings WHERE key = $1", _BOOKS_PROJECTS_SETTING
        )
        if isinstance(stored, dict):
            raw = str(stored.get("val") or "")
    except Exception as exc:  # noqa: BLE001 — a project lookup must never block a task
        logger.warning("hub_project_books_projects_failed", error=str(exc)[:200])
    project = parse_kv(raw).get(entity)
    if not project:
        project = parse_kv(str(getattr(_settings(), "books_todoist_projects", "") or "")).get(
            entity
        )
    return project or None


def _settings() -> Any:
    """Process settings, or None where there are none (tests): the API key
    resolver then falls back to the DB-stored key alone."""
    try:
        from aegis.config import Settings

        return Settings()
    except Exception:  # noqa: BLE001 — pydantic ValidationError without an env
        return None


async def _post_note(pool: asyncpg.Pool, settings: Any, task_id: str, text: str) -> bool:
    """Post one comment. False on any failure; the caller keeps the watermark."""
    try:
        key = await resolve_todoist_api_key(pool, settings or _settings())
        if not key:
            return False
        connector = TodoistConnector(api_key=key, db_pool=pool, timeout=10.0)
        cmd = TodoistConnector.build_note_add_command(task_id, text + FOOTER)
        try:
            result = await connector.commands([cmd])
        finally:
            await connector.close()
        status = TodoistConnector.check_sync_status(result, [cmd["uuid"]])
        if not status["ok"]:
            logger.warning("hub_project_note_failed", task_id=task_id, status=status)
        return bool(status["ok"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("hub_project_note_failed", task_id=task_id, error=str(exc)[:200])
        return False


async def _queue(
    pool: asyncpg.Pool, temp_id: str, command: dict, *, supersede: bool = False
) -> None:
    """Re-armable outbox insert (the `agent_task._queue_command` contract): a
    row already drained to a terminal status is re-armed, a pending one is
    left alone.

    ``supersede`` overwrites a pending row too, and is for a command that
    REPLACES its predecessor rather than adding to it — the status block, whose
    every write is the whole description. Without it the newer block is dropped
    on the floor: the queue keeps the older command, while `block_hash` records
    the new one as written, so no later projection ever queues it again.
    """
    await pool.execute(
        "INSERT INTO todoist_outbox (temp_id, command, status) VALUES ($1, $2, 'pending') "
        "ON CONFLICT (temp_id) DO UPDATE "
        "SET command = EXCLUDED.command, status = 'pending', attempt_count = 0 "
        + ("" if supersede else "WHERE todoist_outbox.status <> 'pending'"),
        temp_id,
        command,
    )


async def _complete_task(pool: asyncpg.Pool, task_id: str) -> bool:
    """Close the task unless the user has claimed it (`@me`): once it is
    theirs, closing it out from under them is worse than leaving it stale."""
    row = await pool.fetchrow(
        "SELECT assignee_label, is_completed FROM todoist_tasks WHERE id = $1", task_id
    )
    # No mirror row means the hub created this task less than a sync ago:
    # nobody can have claimed it yet. Refusing to close it here dropped the
    # close for good, because the projector moves its watermark past the
    # resolve either way — a problem that recovered within five minutes of
    # its task being created left that task open (#473).
    if row is not None and (row["is_completed"] or row["assignee_label"] == "@me"):
        return False
    await _queue(
        pool, f"problem-close-{task_id}", TodoistConnector.build_item_complete_command(task_id)
    )
    await pool.execute(
        "UPDATE todoist_tasks SET is_completed = true, updated_at = now() WHERE id = $1", task_id
    )
    return True


async def _uncomplete_task(pool: asyncpg.Pool, task_id: str) -> bool:
    row = await pool.fetchrow("SELECT is_completed FROM todoist_tasks WHERE id = $1", task_id)
    if row is None or not row["is_completed"]:
        return False
    await _queue(
        pool, f"problem-reopen-{task_id}", TodoistConnector.build_item_uncomplete_command(task_id)
    )
    await pool.execute(
        "UPDATE todoist_tasks SET is_completed = false, updated_at = now() WHERE id = $1", task_id
    )
    return True


async def retire_task(
    pool: asyncpg.Pool, task_id: str, note: str, *, settings: Any = None
) -> bool:
    """Complete a task the hub no longer needs (its problem was merged away),
    with a note saying where the work went. True when the completion queued."""
    await _post_note(pool, settings, task_id, note)
    return await _complete_task(pool, task_id)


async def retire_merged_task(
    pool: asyncpg.Pool, merge: dict[str, Any], *, settings: Any = None
) -> bool | None:
    """Retire the task of a problem `hub.merge_problems` just folded away.

    The merged problem is closed, and a closed problem is never projected
    again, so its task is retired here or never. One implementation for both
    doors onto a merge — the chat tool and the admin Problems page — so they
    cannot drift on what a merge does to Todoist.

    ``merge`` is what `merge_problems` returned. None when there is no task to
    retire: the merged problem had none, or it shares the kept problem's task.
    Otherwise whether the completion queued.
    """
    task_id = str(merge.get("merged_task_id") or "")
    keep_id = str(merge.get("keep_id") or "")
    kept = await get_problem(pool, keep_id) if keep_id else None
    keep_task = str((kept or {}).get("todoist_task_id") or "")
    if not task_id or task_id == keep_task:
        return None
    note = f"Merged into problem {keep_id}" + (f" (task {keep_task})" if keep_task else "") + "."
    return await retire_task(pool, task_id, note, settings=settings)


async def _set_task(pool: asyncpg.Pool, problem_id: str, task_id: str) -> None:
    await pool.execute(
        "UPDATE problems SET todoist_task_id = $2 WHERE id = $1::uuid", problem_id, task_id
    )
    await pool.execute(
        "INSERT INTO problem_links (problem_id, link_kind, ref) VALUES ($1::uuid, 'todoist_task', $2) "
        "ON CONFLICT DO NOTHING",
        problem_id,
        task_id,
    )


async def link_task(pool: asyncpg.Pool, problem_id: str, task_id: str) -> bool:
    """Adopt an existing task (a hand-captured one clarify routed, or the task
    a chat tool was invoked on) as the problem's task, when it has none. The
    watermark moves to the latest event: what happened before the link is on
    the task already in the user's own words. False when the problem already
    has a task."""
    p = await get_problem(pool, problem_id)
    if p is None or p["todoist_task_id"] or not task_id:
        return False
    await _set_task(pool, problem_id, task_id)
    latest = await pool.fetchval(
        "SELECT COALESCE(max(id), 0) FROM problem_events WHERE problem_id = $1::uuid", problem_id
    )
    meta = dict(p["metadata"] or {})
    meta.update(projected_event_id=int(latest), pending_occurrences=0)
    await _save_meta(pool, problem_id, meta)
    return True


async def _save_meta(pool: asyncpg.Pool, problem_id: str, meta: dict[str, Any]) -> None:
    await pool.execute("UPDATE problems SET metadata = $2 WHERE id = $1::uuid", problem_id, meta)


def _times(n: int) -> str:
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def _history_text(kind: str, payload: dict[str, Any]) -> str:
    text = str(payload.get("text") or payload.get("summary") or "").strip()
    head = {
        "investigation": "🔍 Investigation",
        "plan": "🗺 Plan",
        "session_note": "💻 Session",
    }[kind]
    return f"{head}: {text}" if text else head


async def _topic_digest(pool: asyncpg.Pool, problem_id: str, limit: int = 10) -> str:
    """What a topic's round collected, newest first, as the task's description
    (#513): one line per article, linked."""
    rows = await pool.fetch(
        "SELECT payload FROM problem_events "
        "WHERE problem_id = $1::uuid AND kind = 'occurrence' AND payload->>'item' = 'true' "
        "ORDER BY occurred_at DESC, id DESC LIMIT $2",
        problem_id,
        limit,
    )
    lines = []
    for r in rows:
        item = r["payload"] or {}
        title = str(item.get("title") or item.get("url") or "").strip()[:160]
        url = str(item.get("url") or "").strip()
        lines.append(f"- [{title}]({url})" if url else f"- {title}")
    return "New on this topic:\n" + "\n".join(lines) if lines else ""


async def project(
    pool: asyncpg.Pool,
    problem_id: str,
    *,
    settings: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Bring the problem's task up to date with the problem. Idempotent and
    re-runnable; see the module docstring for what one run does."""
    now = now or datetime.now(UTC)
    p = await get_problem(pool, problem_id)
    if p is None:
        return {"problem_id": problem_id, "skipped": "missing"}
    if p["status"] not in PROJECTED_STATUSES:
        return {"problem_id": problem_id, "skipped": p["status"]}
    # A mute silences what a problem does, not its recovery. A live muted
    # problem is left alone — its events, a return included, wait behind the
    # watermark and are told once when the mute ends — but a resolved one
    # still closes its task. Skipping that too kept the task open until the
    # mute ended, and for good when the mute outlasted the close sweep (#473).
    muted = p["muted_until"] is not None and _aware(p["muted_until"], now) > now
    if muted and p["status"] != "resolved":
        return {"problem_id": problem_id, "skipped": "muted"}
    meta: dict[str, Any] = dict(p["metadata"] or {})
    latest_event_id = await pool.fetchval(
        "SELECT COALESCE(max(id), 0) FROM problem_events WHERE problem_id = $1::uuid", problem_id
    )
    # The capture idempotency row is keyed on the tag the task was captured
    # with, so both the lookup below and the capture use the owner's tag.
    owner = await _owner(pool, problem_id)

    task_id = p["todoist_task_id"]
    if task_id and task_id.startswith("item-"):
        # Created through the outbox. Until TodoistSyncFlow drains it there is
        # nothing to comment on. The drain writes the real id on the outbox
        # row (`committed_id`) and never on the capture idempotency row, so
        # waiting for the idempotency row alone waited for ever: the task was
        # created and never commented on or closed (#473, prod 92cdd766).
        real = await pool.fetchval(
            "SELECT todoist_task_ref FROM todoist_capture_idempotency "
            "WHERE source_tag = $1 AND external_id = $2",
            owner.source_tag,
            f"problem-{problem_id}",
        )
        if not real or real.startswith("item-"):
            real = await pool.fetchval(
                "SELECT committed_id FROM todoist_outbox "
                "WHERE temp_id = $1 AND status = 'committed'",
                task_id,
            )
        if not real or real.startswith("item-"):
            return {"problem_id": problem_id, "task_id": task_id, "skipped": "task_pending_outbox"}
        task_id = real
        await _set_task(pool, problem_id, task_id)

    if not task_id and p["class"] == TOPIC_CLASS and not meta.get("attention"):
        # A tracked topic's round of news earns a task only once it holds
        # enough items (`research_topics.ATTENTION_ITEMS`, #513). Until then it
        # lives in the hub and Raphael's briefing. Its events are marked seen,
        # so the sweep does not come back for them; the task, when the round
        # crosses its threshold, lists the round's items itself.
        meta.update(projected_event_id=int(latest_event_id), pending_occurrences=0)
        await _save_meta(pool, problem_id, meta)
        return {"problem_id": problem_id, "skipped": "below_attention"}

    if not task_id and p["status"] == "resolved":
        # It came and went before it earned a task: seen only inside a deploy
        # window, or its inline projection failed and it recovered before the
        # sweep. A task born closed tells nobody anything, so the events are
        # marked seen and nothing is created. Creating one moved the
        # watermark past the resolve, and that task never closed (#473). A
        # later occurrence reopens the problem, and THAT projects a task.
        meta.update(projected_event_id=int(latest_event_id), pending_occurrences=0)
        await _save_meta(pool, problem_id, meta)
        return {"problem_id": problem_id, "skipped": "resolved_without_task"}

    async with pool.acquire() as conn:
        window = await _active_suppression(conn, p["subject"], p["subject_kind"], now)
    links = [
        dict(r)
        for r in await pool.fetch(
            "SELECT link_kind, ref FROM problem_links WHERE problem_id = $1::uuid ORDER BY created_at",
            problem_id,
        )
    ]
    sessions = await work_sessions.list_for_task(pool, task_id) if task_id else []
    block = render_block(
        p,
        window=dict(window) if window else None,
        links=links,
        sessions=sessions,
        steps=await _step_progress(pool, problem_id),
    )

    if not task_id:
        if p["class"] == TOPIC_CLASS:
            # A topic's task is about its round, not its latest article: list
            # what the round collected (#513).
            latest = await _topic_digest(pool, problem_id)
        else:
            latest = await pool.fetchval(
                "SELECT payload->>'description' FROM problem_events "
                "WHERE problem_id = $1::uuid AND kind = 'occurrence' ORDER BY id DESC LIMIT 1",
                problem_id,
            )
        description = merge_block((latest or "")[: _DESCRIPTION_CAP - len(block) - 2], block)
        # ponytail: every money problem goes to the personal project. Routing a
        # hikmah instrument to the hikmah project needs the chart's
        # instrument→entity map, which nothing here has yet.
        project_id = (
            await _books_project(pool, owner.books_entity) if owner.books_entity else None
        )
        task_id = await _capture_to_inbox_impl(
            pool,
            owner.source_tag,
            f"problem-{problem_id}",
            p["title"][:120],
            description[:_DESCRIPTION_CAP],
            [
                await _assignee_label(pool, owner.agent_tag, owner.fallback_label),
                *owner.extra_labels,
            ],
            project_id=project_id,
        )
        if not task_id:
            return {"problem_id": problem_id, "skipped": "no_task"}
        await _set_task(pool, problem_id, task_id)
        # The task's own description says everything up to now: history
        # before creation is not replayed as comments.
        meta.update(
            projected_event_id=int(latest_event_id),
            pending_occurrences=0,
            block_hash=hashlib.sha1(block.encode()).hexdigest(),
            projected_title=p["title"],
        )
        await _save_meta(pool, problem_id, meta)
        logger.info("hub_project_task_created", problem_id=problem_id, task_id=task_id)
        return {"problem_id": problem_id, "task_id": task_id, "created": True, "comments": 0}

    since = int(meta.get("projected_event_id") or 0)
    events = await pool.fetch(
        "SELECT id, kind, payload, occurred_at FROM problem_events "
        "WHERE problem_id = $1::uuid AND id > $2 ORDER BY id",
        problem_id,
        since,
    )
    # Under a mute an occurrence is counted on the problem and told nowhere,
    # including the ones still waiting for the collapse window: the task is
    # about to close, and "3 more occurrences" is exactly what the mute is for.
    pending = 0 if muted else int(meta.get("pending_occurrences") or 0)
    comments: list[str] = []
    close = reopen = renamed = False
    # Resolves and returns are told as where the batch ENDS, once. One comment
    # per turn let a backlog burst out: 586cbacb had five resolves and four
    # reopens waiting behind its mute, ten comments the moment it ended.
    turn: asyncpg.Record | None = None
    cleared = returned = 0
    for e in events:
        payload = e["payload"] or {}
        if e["kind"] == "occurrence":
            if not payload.get("suppressed_by") and not muted:
                pending += 1
        elif e["kind"] == "state_change":
            action = payload.get("action")
            if action == "grouped":
                members = [str(m) for m in (payload.get("members") or []) if m]
                shown = ", ".join(members[:8]) + ("…" if len(members) > 8 else "")
                comments.append(
                    f"🧩 Same thing on {payload.get('member_count') or len(members)} "
                    f"{p['subject_kind'] or 'subject'}s, so this task now covers all of them"
                    + (f": {shown}." if shown else ".")
                    + " The others were folded in and closed; the next one lands here."
                )
                renamed = True
            elif action == "resolve":
                turn, close, reopen = e, True, False
                cleared += 1
            elif action in {"reopen", "promote"}:
                turn, close, reopen = e, False, True
                # A promote after a reopen inside a deploy window is the same
                # return, told twice by the hub; only a reopen counts one.
                returned += action == "reopen"
        elif e["kind"] in _HISTORY_KINDS and not payload.get("posted"):
            comments.append(_history_text(e["kind"], payload))
        if e["kind"] == "plan":
            steps = plan_steps(payload)
            if steps:
                await _create_plan_steps(pool, settings, problem_id, task_id, steps)
        done = payload.get("step_done")
        if isinstance(done, int) and done > 0:
            await _complete_step(pool, problem_id, done)

    if muted and reopen:
        # The problem was resolved when it was read and has come back since.
        # A return under a mute waits for the mute to end, like any other.
        return {"problem_id": problem_id, "task_id": task_id, "skipped": "muted"}
    if turn is not None:
        at = _ts(turn["occurred_at"])
        text = ""
        if close:
            already = await pool.fetchval(
                "SELECT is_completed FROM todoist_tasks WHERE id = $1", task_id
            )
            if not already:
                text = f"✅ Resolved at {at}. Closing this task."
            elif not muted:
                # A person completed it (and this resolve is that completion,
                # read back), or the hub did on an earlier resolve and the
                # return in between never reached the task. Nothing to close.
                # Under a mute there is nothing worth saying at all.
                text = f"✅ Resolved at {at}. This task was already completed."
            if text and returned:
                text += f" It came back {_times(returned)} since the last update."
        else:
            why = (
                "seen during a deploy window and still failing after it"
                if (turn["payload"] or {}).get("action") == "promote"
                else "recurred inside the reopen window"
            )
            text = f"🔁 Back at {at}: {why}."
            if cleared:
                text += f" It had cleared {_times(cleared)} since the last update."
        if text:
            comments.append(text)

    last_comment_at = meta.get("last_occurrence_comment_at")
    if pending and (
        not last_comment_at
        or now - datetime.fromisoformat(str(last_comment_at)) >= COLLAPSE_WINDOW
    ):
        comments.insert(
            0,
            # A topic's occurrences are articles, not failures (#513).
            f"📰 {pending} new item{'s' if pending != 1 else ''} on this topic; "
            f"latest {_ts(p['last_seen_at'])}."
            if p["class"] == TOPIC_CLASS
            else f"⚠️ {pending} more occurrence{'s' if pending != 1 else ''} "
            f"({p['occurrences']} in total); last seen {_ts(p['last_seen_at'])}.",
        )
        pending = 0
        meta["last_occurrence_comment_at"] = now.isoformat()

    posted = 0
    for text in comments:
        if not await _post_note(pool, settings, task_id, text):
            break
        posted += 1
    if posted < len(comments):
        # Leave the watermark: the next sweep re-derives and retries.
        logger.warning("hub_project_partial", problem_id=problem_id, posted=posted, of=len(comments))
        return {"problem_id": problem_id, "task_id": task_id, "comments": posted, "partial": True}

    if close:
        await _complete_task(pool, task_id)
    elif reopen:
        await _uncomplete_task(pool, task_id)

    # Re-read after the loop: a plan just created its subtasks, and a step
    # just ticked off changes the count the block reports.
    progress = await _step_progress(pool, problem_id)
    if progress and f"Steps: {progress}" not in block:
        block = render_block(
            p,
            window=dict(window) if window else None,
            links=links,
            sessions=sessions,
            steps=progress,
        )
    # The problem's title only changes when the hub rewrites it — today that
    # is a group upgrade, where "Post cms4k… stuck in Postiz" has to become
    # "6 posts stuck in Postiz" or the task lies about its own scope. A task
    # projected before this existed has no recorded title, so it is renamed
    # only on an event that actually renamed the problem; nothing else
    # overwrites a title a person may have edited.
    prior_title = meta.get("projected_title")
    rename = renamed or (prior_title is not None and prior_title != p["title"])
    block_hash = hashlib.sha1(block.encode()).hexdigest()
    if meta.get("block_hash") != block_hash or rename:
        current = await pool.fetchval("SELECT description FROM todoist_tasks WHERE id = $1", task_id)
        fields: dict[str, Any] = {
            "description": merge_block(current, block)[:_DESCRIPTION_CAP]
        }
        if rename:
            fields["content"] = p["title"][:120]
            meta["projected_title"] = p["title"]
        await _queue(
            pool,
            f"problem-desc-{task_id}",
            TodoistConnector.build_item_update_command(task_id, **fields),
            supersede=True,
        )
        meta["block_hash"] = block_hash

    meta.update(
        projected_event_id=int(events[-1]["id"]) if events else max(since, 0),
        pending_occurrences=pending,
    )
    await _save_meta(pool, problem_id, meta)
    return {"problem_id": problem_id, "task_id": task_id, "created": False, "comments": posted}


async def project_pending(
    pool: asyncpg.Pool,
    *,
    settings: Any = None,
    limit: int = 50,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Every problem whose task is missing, still an outbox temp id, or behind
    its events — newest activity first. Run by `HubSweepFlow`.

    A muted problem is swept once it has resolved, so its task closes under
    the mute; a resolved problem with no task is swept once, to mark its
    events seen, and never gets one."""
    now = now or datetime.now(UTC)
    rows = await pool.fetch(
        "SELECT id::text AS id FROM problems p "
        "WHERE p.closed_at IS NULL AND p.status = ANY($1::text[]) "
        "  AND (p.muted_until IS NULL OR p.muted_until <= $2 OR p.status = 'resolved') "
        # A topic's round below its threshold has nothing to project (#513):
        # left in, a busy day's news would crowd real problems out of LIMIT.
        "  AND NOT (p.class = $4 AND p.todoist_task_id IS NULL "
        "           AND COALESCE(p.metadata->>'attention', '') <> 'true') "
        "  AND ((p.todoist_task_id IS NULL AND p.status <> 'resolved') "
        "       OR p.todoist_task_id LIKE 'item-%' "
        "       OR COALESCE((p.metadata->>'pending_occurrences')::int, 0) > 0 "
        "       OR EXISTS (SELECT 1 FROM problem_events e WHERE e.problem_id = p.id "
        "                  AND e.id > COALESCE((p.metadata->>'projected_event_id')::bigint, 0))) "
        "ORDER BY p.last_seen_at DESC LIMIT $3",
        sorted(PROJECTED_STATUSES),
        now,
        limit,
        TOPIC_CLASS,
    )
    out = []
    for r in rows:
        try:
            out.append(await project(pool, r["id"], settings=settings, now=now))
        except Exception as exc:  # noqa: BLE001 — one bad problem must not stop the sweep
            logger.warning("hub_project_failed", problem_id=r["id"], error=str(exc)[:200])
            out.append({"problem_id": r["id"], "error": str(exc)[:200]})
    return out


async def reconcile_completed_tasks(
    pool: asyncpg.Pool, *, limit: int = 50, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Make a completed task and its live problem agree again. Run by
    `HubSweepFlow`, before projection, so what changes reaches the task in
    the same tick. Returns one row per problem touched, with ``action``
    `resolved` or `task_reopened`.

    Nothing used to read a completion back: a task ticked off in Todoist, or
    through the `complete_task` chat tool, left its problem live for good —
    counted as open on the Problems page and in the digest, and with the
    heartbeat's re-investigation skipping `waiting_human`, never looked at
    again (#473). The completion is somebody's word, and the question is
    whose, and about what:

    * **Completed after the problem's last return** (or it never returned):
      a person — or an agent acting on a person's decision — finished the
      work. The problem resolves through the hub's own transition. If it is
      not over, the next occurrence reopens it and the projector reopens
      the task.
    * **Completed before the last return, which the projector has already
      told:** the completion is the hub's own close, and the reopen that
      followed never stuck. TodoistSyncFlow applies Todoist's changes before
      it drains the outbox, so the mirror can be a tick stale either way: the
      projector's reopen can find it still "open" and send nothing (prod
      2140a366), or a close drained on one tick comes back as "completed" on
      the next, after the reopen. The task is reopened, which is what the
      projector meant to do. Taking this for a person's completion would
      swallow the return.
    * **A return the projector has not told yet** — under a mute, or inside a
      deploy window — is left alone: the task is still the one the hub
      closed, and the projector reopens it when it may.

    Group problems and `manual` ones (hand-written `@code` tasks) follow the
    same rules. An `item-…` ref is a capture the sync has not drained, so
    there is no real task behind it to have been completed.
    """
    now = now or datetime.now(UTC)
    rows = await pool.fetch(
        "SELECT p.id::text AS id, p.status, p.todoist_task_id, p.class, "
        "       EXISTS (SELECT 1 FROM problem_events e WHERE e.problem_id = p.id "
        "               AND e.kind = 'state_change' "
        "               AND e.payload->>'action' IN ('reopen', 'promote') "
        "               AND e.created_at > t.completed_at) AS came_back_since "
        "FROM problems p JOIN todoist_tasks t ON t.id = p.todoist_task_id "
        "WHERE p.closed_at IS NULL AND p.status = ANY($1::text[]) AND t.is_completed "
        "  AND p.todoist_task_id NOT LIKE 'item-%' "
        "  AND NOT EXISTS (SELECT 1 FROM problem_events e WHERE e.problem_id = p.id "
        "        AND e.kind = 'state_change' AND e.payload->>'action' IN ('reopen', 'promote') "
        "        AND e.id > COALESCE((p.metadata->>'projected_event_id')::bigint, 0)) "
        "ORDER BY p.last_seen_at DESC LIMIT $2",
        sorted(LIVE_STATUSES),
        limit,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        row = {"problem_id": r["id"], "task_id": r["todoist_task_id"], "was": r["status"]}
        try:
            if r["came_back_since"]:
                if await _uncomplete_task(pool, r["todoist_task_id"]):
                    out.append({**row, "action": "task_reopened"})
            elif r["class"] == TOPIC_CLASS:
                # A ticked-off topic task means "seen" (#513): the round
                # resolves AND closes at once, so the next article opens a
                # fresh one. `close_round` does both under the lock
                # `ingest_event` takes for the round's key, so an item attached
                # in between cannot reopen the task the user just dismissed.
                from aegis.services.research_topics import close_round

                if await close_round(
                    pool,
                    r["id"],
                    reason=TASK_COMPLETED_REASON,
                    source=TASK_COMPLETED_SOURCE,
                    now=now,
                ):
                    out.append({**row, "action": "resolved"})
            elif await set_status(
                pool,
                r["id"],
                "resolved",
                reason=TASK_COMPLETED_REASON,
                source=TASK_COMPLETED_SOURCE,
                now=now,
            ):
                out.append({**row, "action": "resolved"})
        except Exception as exc:  # noqa: BLE001 — one bad problem must not stop the sweep
            logger.warning("hub_task_completion_failed", problem_id=r["id"], error=str(exc)[:200])
    if out:
        logger.info(
            "hub_task_completions_reconciled",
            resolved=sum(1 for o in out if o["action"] == "resolved"),
            task_reopened=sum(1 for o in out if o["action"] == "task_reopened"),
        )
    return out
