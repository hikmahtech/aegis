"""Projection of a problem onto its human surface: the Todoist task.

Spec: docs/superpowers/specs/2026-09-07-problem-hub-design.md §6.

The problem (`services/hub.py`) is the record; the task is a *view* of it.
Nothing here is ever read back — the status block in the task description is
for the human and for a session that has only the task in front of it, and the
only thing that flows from Todoist into AEGIS is a user's own comment, over the
webhook that already exists.

What a projection does, in order:

1. **Ensures the task** once the problem is worth a human's attention: any
   projected status (§4), not suppressed, not muted. Created through the same
   idempotent capture the chat tools use, with `external_id = problem-<id>`,
   so a retried projection finds its own task.
2. **Comments, never new tasks.** Every event since the last projection
   becomes a comment — except occurrences, which are collapsed to one
   "N more" comment per ``COLLAPSE_WINDOW`` so a flapping service cannot flood
   the thread. A `resolve` transition closes the task (unless the user has
   claimed it with `@me`); a `reopen` or `promote` reopens it.
3. **Re-renders the status block** in the description, replaced whole between
   its markers, only when its content changed.

Comments carry the ``Workflow run:`` token so `is_user_note` and clarify keep
excluding them, and a comment that fails to post leaves the watermark where it
was: the next sweep retries rather than the event being lost.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import structlog

from aegis.connectors.todoist import TodoistConnector
from aegis.services.agents import resolve_tag
from aegis.services.hub import _active_suppression, _aware, get_problem
from aegis.services.todoist_config import resolve_todoist_api_key
from aegis.services.tools.gtd import _capture_to_inbox_impl

logger = structlog.get_logger()

# The literal token clarify's loop guard and `task_sessions.is_user_note` key on.
FOOTER = "\n\nWorkflow run: problem-hub"
SOURCE_TAG = "#alert"
COLLAPSE_WINDOW = timedelta(minutes=30)
# Statuses that earn a task. `suppressed` and `closed` never do.
PROJECTED_STATUSES = frozenset(
    {"open", "investigating", "waiting_human", "fixing", "verifying", "resolved"}
)
_BLOCK_RE = re.compile(r"<!-- aegis:problem [^>]*-->.*?<!-- /aegis:problem -->", re.S)
_HISTORY_KINDS = frozenset({"investigation", "plan", "session_note", "human_note"})
_FALLBACK_LABEL = "@pandora"
_DESCRIPTION_CAP = 2000


def _ts(value: Any) -> str:
    if isinstance(value, datetime):
        return f"{_aware(value, value):%Y-%m-%d %H:%M} UTC"
    return str(value or "")


def render_block(
    problem: dict[str, Any],
    *,
    window: dict[str, Any] | None = None,
    links: list[dict[str, Any]] | None = None,
) -> str:
    """The status block for a task description. Pure."""
    lines = [
        f"<!-- aegis:problem {problem['id']} -->",
        f"Status: {problem['status']} · seen {problem['occurrences']}× since "
        f"{_ts(problem['first_seen_at'])} · last {_ts(problem['last_seen_at'])}",
        f"Subject: {problem['subject'] or '-'} ({problem['subject_kind'] or '-'}) · "
        f"{problem['severity']} · class {problem['class']}",
    ]
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
    lines.append("<!-- /aegis:problem -->")
    return "\n".join(lines)


def merge_block(description: str | None, block: str) -> str:
    """Replace the existing block in ``description`` or append one. The user's
    text around it is never touched."""
    base = description or ""
    if _BLOCK_RE.search(base):
        return _BLOCK_RE.sub(lambda _m: block, base, count=1)
    return (base.rstrip() + "\n\n" + block) if base.strip() else block


async def _assignee_label(pool: asyncpg.Pool) -> str:
    """The label that assigns the task to the `infra` agent — its first mention
    alias — falling back to the shipped default. Never raises."""
    try:
        agent_id = await resolve_tag(pool, "infra")
        if not agent_id:
            return _FALLBACK_LABEL
        meta = await pool.fetchval("SELECT metadata FROM agents WHERE id = $1", agent_id)
        aliases = (meta or {}).get("mention_aliases") or [agent_id]
        return f"@{str(aliases[0]).lstrip('@')}"
    except Exception as exc:  # noqa: BLE001 — a label lookup must never block a task
        logger.warning("hub_project_label_failed", error=str(exc)[:200])
        return _FALLBACK_LABEL


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


async def _queue(pool: asyncpg.Pool, temp_id: str, command: dict) -> None:
    """Re-armable outbox insert (the `agent_task._queue_command` contract): a
    row already drained to a terminal status is re-armed, a pending one is
    left alone."""
    await pool.execute(
        "INSERT INTO todoist_outbox (temp_id, command, status) VALUES ($1, $2, 'pending') "
        "ON CONFLICT (temp_id) DO UPDATE "
        "SET command = EXCLUDED.command, status = 'pending', attempt_count = 0 "
        "WHERE todoist_outbox.status <> 'pending'",
        temp_id,
        command,
    )


async def _complete_task(pool: asyncpg.Pool, task_id: str) -> bool:
    """Close the task unless the user has claimed it (`@me`): once it is
    theirs, closing it out from under them is worse than leaving it stale."""
    row = await pool.fetchrow(
        "SELECT assignee_label, is_completed FROM todoist_tasks WHERE id = $1", task_id
    )
    if row is None or row["is_completed"] or row["assignee_label"] == "@me":
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


def _history_text(kind: str, payload: dict[str, Any]) -> str:
    text = str(payload.get("text") or payload.get("summary") or "").strip()
    head = {
        "investigation": "🔍 Investigation",
        "plan": "🗺 Plan",
        "session_note": "💻 Session",
        "human_note": "🗒 Note",
    }[kind]
    return f"{head}: {text}" if text else head


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
    if p["muted_until"] is not None and _aware(p["muted_until"], now) > now:
        return {"problem_id": problem_id, "skipped": "muted"}
    meta: dict[str, Any] = dict(p["metadata"] or {})
    latest_event_id = await pool.fetchval(
        "SELECT COALESCE(max(id), 0) FROM problem_events WHERE problem_id = $1::uuid", problem_id
    )

    task_id = p["todoist_task_id"]
    if task_id and task_id.startswith("item-"):
        # Created through the outbox: the real id lands on the idempotency row
        # once TodoistSyncFlow drains it. Until then there is nothing to
        # comment on.
        real = await pool.fetchval(
            "SELECT todoist_task_ref FROM todoist_capture_idempotency "
            "WHERE source_tag = $1 AND external_id = $2",
            SOURCE_TAG,
            f"problem-{problem_id}",
        )
        if not real or real.startswith("item-"):
            return {"problem_id": problem_id, "task_id": task_id, "skipped": "task_pending_outbox"}
        task_id = real
        await _set_task(pool, problem_id, task_id)

    async with pool.acquire() as conn:
        window = await _active_suppression(conn, p["subject"], p["subject_kind"], now)
    links = [
        dict(r)
        for r in await pool.fetch(
            "SELECT link_kind, ref FROM problem_links WHERE problem_id = $1::uuid ORDER BY created_at",
            problem_id,
        )
    ]
    block = render_block(p, window=dict(window) if window else None, links=links)

    if not task_id:
        latest = await pool.fetchval(
            "SELECT payload->>'description' FROM problem_events "
            "WHERE problem_id = $1::uuid AND kind = 'occurrence' ORDER BY id DESC LIMIT 1",
            problem_id,
        )
        description = merge_block((latest or "")[: _DESCRIPTION_CAP - len(block) - 2], block)
        task_id = await _capture_to_inbox_impl(
            pool,
            SOURCE_TAG,
            f"problem-{problem_id}",
            p["title"][:120],
            description[:_DESCRIPTION_CAP],
            [await _assignee_label(pool)],
        )
        if not task_id:
            return {"problem_id": problem_id, "skipped": "no_task"}
        await _set_task(pool, problem_id, task_id)
        # The task's own description says everything up to now: history
        # before creation is not replayed as comments.
        meta.update(
            projected_event_id=int(latest_event_id),
            projected_at=now.isoformat(),
            pending_occurrences=0,
            block_hash=hashlib.sha1(block.encode()).hexdigest(),
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
    pending = int(meta.get("pending_occurrences") or 0)
    comments: list[str] = []
    close = reopen = False
    for e in events:
        payload = e["payload"] or {}
        if e["kind"] == "occurrence":
            if not payload.get("suppressed_by"):
                pending += 1
        elif e["kind"] == "state_change":
            action = payload.get("action")
            if action == "resolve":
                comments.append(f"✅ Resolved at {_ts(e['occurred_at'])}. Closing this task.")
                close, reopen = True, False
            elif action in {"reopen", "promote"}:
                why = (
                    "seen during a deploy window and still failing after it"
                    if action == "promote"
                    else "recurred inside the reopen window"
                )
                comments.append(f"🔁 Back at {_ts(e['occurred_at'])}: {why}.")
                reopen, close = True, False
        elif e["kind"] in _HISTORY_KINDS and not payload.get("posted"):
            comments.append(_history_text(e["kind"], payload))

    last_comment_at = meta.get("last_occurrence_comment_at")
    if pending and (
        not last_comment_at
        or now - datetime.fromisoformat(str(last_comment_at)) >= COLLAPSE_WINDOW
    ):
        comments.insert(
            0,
            f"⚠️ {pending} more occurrence{'s' if pending != 1 else ''} "
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

    block_hash = hashlib.sha1(block.encode()).hexdigest()
    if meta.get("block_hash") != block_hash:
        current = await pool.fetchval("SELECT description FROM todoist_tasks WHERE id = $1", task_id)
        await _queue(
            pool,
            f"problem-desc-{task_id}",
            TodoistConnector.build_item_update_command(
                task_id, description=merge_block(current, block)[:_DESCRIPTION_CAP]
            ),
        )
        meta["block_hash"] = block_hash

    meta.update(
        projected_event_id=int(events[-1]["id"]) if events else max(since, 0),
        projected_at=now.isoformat(),
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
    its events — newest activity first. Run by `HubSweepFlow`."""
    now = now or datetime.now(UTC)
    rows = await pool.fetch(
        "SELECT id::text AS id FROM problems p "
        "WHERE p.closed_at IS NULL AND p.status = ANY($1::text[]) "
        "  AND (p.muted_until IS NULL OR p.muted_until <= $2) "
        "  AND (p.todoist_task_id IS NULL OR p.todoist_task_id LIKE 'item-%' "
        "       OR COALESCE((p.metadata->>'pending_occurrences')::int, 0) > 0 "
        "       OR EXISTS (SELECT 1 FROM problem_events e WHERE e.problem_id = p.id "
        "                  AND e.id > COALESCE((p.metadata->>'projected_event_id')::bigint, 0))) "
        "ORDER BY p.last_seen_at DESC LIMIT $3",
        sorted(PROJECTED_STATUSES),
        now,
        limit,
    )
    out = []
    for r in rows:
        try:
            out.append(await project(pool, r["id"], settings=settings, now=now))
        except Exception as exc:  # noqa: BLE001 — one bad problem must not stop the sweep
            logger.warning("hub_project_failed", problem_id=r["id"], error=str(exc)[:200])
            out.append({"problem_id": r["id"], "error": str(exc)[:200]})
    return out
