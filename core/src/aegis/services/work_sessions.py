"""work_sessions — who is on which task: AEGIS's coding turns and the operator's own sessions.

One row per session on a task. AEGIS holds at most one live row per task
(`owner='aegis'`, enforced by the partial unique index `work_sessions_task_aegis`);
the operator gets a row per account they report from (`owner='operator'`,
written by the `report_progress` tool). The comment thread on the task is the
control channel for the AEGIS row; this module owns the row that lets a later
comment resume the same session in the same worktree, and the start-or-signal
dispatch that lands every comment on the task's single workflow.

Two things are deliberate here:

* ``session_id`` is minted once, by ``create_session``'s ``ON CONFLICT DO
  NOTHING``. A second caller for the same task gets the *existing* row back, so
  two comments arriving together can never fork a task into two sessions.
* ``find_turns_due`` compares every **user** note against the row's own
  watermark and hands back all of them, joined oldest-first. AEGIS's own notes
  are excluded in SQL rather than in Python for the same reason ClarifyFlow does
  it (see ``aegis.clarify_note``): a machine note that counted as a turn would
  make the task answer itself forever.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from temporalio.exceptions import WorkflowAlreadyStartedError

from aegis.clarify_note import AGENT_REPLY_PREFIX, CLARIFY_NOTE_PREFIX

OWNERS = frozenset({"aegis", "operator"})
STATUSES = frozenset({"active", "parked", "done"})

# An operator row counts as "in the session" for this long after it was last
# reported or seen live. Past it the collision check proceeds; the sweep's
# liveness cross-check parks the row.
OPERATOR_LIVE_WINDOW = timedelta(minutes=30)

# session_id and the uuids are uuid columns; every consumer wants the string form.
_COLS = (
    "id::text AS id, task_id, agent_id, session_id::text AS session_id, "
    "problem_id::text AS problem_id, repo, github_repo, worktree_path, branch, host, "
    "account, engine, owner, status, summary, last_output_file, last_host, slack_ref, "
    "turns, last_turn_at, last_seen_at, created_at"
)
# The AEGIS row every per-task call means: the one live coding session.
_AEGIS_LIVE = "owner = 'aegis' AND status <> 'done'"

# EVERY user note on a live task that arrived after the last turn we ran for
# it, joined oldest-first into one comment. `created_at` is the watermark until
# the first turn, so the comment that created the session does not immediately
# re-fire it.
#
# All of them, not just the newest: this is the fallback for a webhook that
# never arrived, and a webhook outage drops a RUN of comments, not one. Handing
# back only the last would answer the final message of a conversation the turn
# never read. The aggregate always produces a row, so `comment IS NOT NULL` is
# what keeps a task with nothing new out of the result — one row per task, and
# never an empty comment. Longest wait first: `waiting_since` is the OLDEST
# unanswered note, so a starved task cannot be pushed past the limit by a task
# that was commented on more recently.
_TURNS_DUE_SQL = f"""
SELECT ts.task_id, ts.agent_id, n.comment
FROM work_sessions ts
JOIN todoist_tasks t ON t.id = ts.task_id AND NOT t.is_completed
JOIN LATERAL (
    SELECT string_agg(content, E'\\n\\n' ORDER BY posted_at, id) AS comment,
           min(posted_at) AS waiting_since
    FROM todoist_notes
    WHERE item_id = ts.task_id
      AND content NOT LIKE $1 AND content NOT LIKE $2
      AND content NOT LIKE '%Workflow run:%'
      AND posted_at > COALESCE(ts.last_turn_at, ts.created_at)
) n ON n.comment IS NOT NULL
WHERE ts.{_AEGIS_LIVE}
ORDER BY n.waiting_since ASC
LIMIT $3
"""


def is_user_note(content: str) -> bool:
    """False for AEGIS's own comments — the clarify/agent-reply prefixes, and any
    note carrying the `Workflow run:` footer a coding turn posts."""
    c = content or ""
    return not (
        c.startswith(CLARIFY_NOTE_PREFIX)
        or c.startswith(AGENT_REPLY_PREFIX)
        or "Workflow run:" in c
    )


def _uuid_or_none(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value or "").strip())
    except ValueError:
        return None


async def get_session(pool: Any, task_id: str) -> dict | None:
    """The task's live AEGIS coding session, or None."""
    row = await pool.fetchrow(
        f"SELECT {_COLS} FROM work_sessions WHERE task_id = $1 AND {_AEGIS_LIVE}", task_id
    )
    return dict(row) if row else None


async def list_for_task(pool: Any, task_id: str) -> list[dict]:
    """Every session row on the task, AEGIS's first, then oldest first."""
    rows = await pool.fetch(
        f"SELECT {_COLS} FROM work_sessions WHERE task_id = $1 "
        "ORDER BY (owner = 'aegis') DESC, created_at",
        task_id,
    )
    return [dict(r) for r in rows]


async def create_session(pool: Any, *, task_id: str, agent_id: str) -> dict:
    """The task's AEGIS session row, creating it (with a fresh session id) if absent.

    Idempotent: a task already holding a live session keeps its `session_id`,
    so a concurrent second caller never mints a competing session. The row
    inherits the task's problem through its `todoist_task` link, when the hub
    projected the task.
    """
    await pool.execute(
        "INSERT INTO work_sessions (task_id, agent_id, session_id, problem_id, last_seen_at) "
        "VALUES ($1, $2, $3, (SELECT problem_id FROM problem_links "
        "WHERE link_kind = 'todoist_task' AND ref = $1 ORDER BY created_at DESC LIMIT 1), now()) "
        "ON CONFLICT (task_id) WHERE owner = 'aegis' AND status <> 'done' DO NOTHING",
        task_id,
        agent_id,
        uuid.uuid4(),
    )
    row = await get_session(pool, task_id)
    if row is None:  # pragma: no cover — the insert above guarantees a row
        raise RuntimeError(f"work_sessions row vanished for task {task_id}")
    return row


async def set_repo(
    pool: Any,
    task_id: str,
    *,
    repo: str,
    github_repo: str,
    worktree_path: str,
    branch: str,
    host: str,
) -> None:
    """Record where the session's checkout lives, once it has been resolved."""
    await pool.execute(
        "UPDATE work_sessions SET repo = $2, github_repo = $3, worktree_path = $4, "
        f"branch = $5, host = $6 WHERE task_id = $1 AND {_AEGIS_LIVE}",
        task_id,
        repo,
        github_repo,
        worktree_path,
        branch,
        host,
    )


async def set_last_run(
    pool: Any,
    task_id: str,
    *,
    output_file: str,
    host: str,
    account: str = "",
    engine: str = "",
) -> None:
    """Record the turn we just launched: where it writes, and the account and
    engine it runs under.

    The output file is what separates "an orphaned turn of ours is still
    writing" from "the last turn ended" — `check_task_collision` probes it. The
    account is what the NEXT turn's `--resume` must run under: a session
    resumed on a different profile is a fresh, amnesiac one. An empty account or
    engine keeps what the row has, so a launch that could not say does not
    erase what an earlier one did.
    """
    await pool.execute(
        "UPDATE work_sessions SET last_output_file = $2, last_host = $3, "
        "account = COALESCE(NULLIF($4, ''), account), "
        "engine = COALESCE(NULLIF($5, ''), engine), "
        "status = 'active', last_seen_at = now() "
        f"WHERE task_id = $1 AND {_AEGIS_LIVE}",
        task_id,
        output_file,
        host,
        account,
        engine,
    )


async def set_state(pool: Any, task_id: str, *, status: str, summary: str) -> bool:
    """What the AEGIS session is doing now and why — `park_task` writes the
    park reason here, so the registry says it and not only the worker log.
    True when a live row moved."""
    if status not in STATUSES:
        raise ValueError(f"unknown session status {status!r}")
    tag = await pool.execute(
        "UPDATE work_sessions SET status = $2, summary = $3, last_seen_at = now() "
        f"WHERE task_id = $1 AND {_AEGIS_LIVE}",
        task_id,
        status,
        (summary or "")[:500],
    )
    return _rows_affected(tag) > 0


async def record_turn(pool: Any, task_id: str, *, launched: bool) -> bool:
    """Move the watermark past the comment we just consumed. True when a row moved.

    `last_turn_at` moves either way — a comment we looked at and did not act on
    must not be re-picked forever — but only a turn that actually launched a
    session counts towards `turns`.

    False means no row matched: the session was cleaned up (or never created)
    while the turn was running. Reporting that as a recorded turn would claim a
    watermark that does not exist, and the caller would stop looking for the
    reason its comment keeps coming back.
    """
    tag = await pool.execute(
        "UPDATE work_sessions SET last_turn_at = now(), turns = turns + $2 "
        f"WHERE task_id = $1 AND {_AEGIS_LIVE}",
        task_id,
        1 if launched else 0,
    )
    return _rows_affected(tag) > 0


def _rows_affected(tag: Any) -> int:
    """Row count from an asyncpg command tag (`"UPDATE 1"`); 0 when unreadable."""
    parts = str(tag or "").split()
    return int(parts[-1]) if parts and parts[-1].isdigit() else 0


async def set_slack_ref(pool: Any, task_id: str, ref: dict) -> None:
    """Remember the Slack thread root so replies land in the same thread.

    `ref` is passed as a dict, NOT a `json.dumps` string: the pool installs a
    jsonb codec that serializes for us and rejects pre-dumped payloads.
    """
    await pool.execute(
        f"UPDATE work_sessions SET slack_ref = $2 WHERE task_id = $1 AND {_AEGIS_LIVE}",
        task_id,
        ref,
    )


async def find_by_thread(pool: Any, channel: str, ts: str) -> str | None:
    """The task whose session owns this Slack thread root, or None."""
    row = await pool.fetchrow(
        "SELECT task_id FROM work_sessions "
        "WHERE slack_ref->>'channel' = $1 AND slack_ref->>'ts' = $2",
        channel,
        ts,
    )
    return row["task_id"] if row else None


async def find_turns_due(pool: Any, limit: int = 20) -> list[dict]:
    """Sessions with unanswered user comments: `[{task_id, agent_id, comment}]`.

    `comment` carries EVERY note posted since the watermark, oldest first,
    joined by a blank line — the shape `_later_turn_prompt` already quotes for a
    batch of comments drained from the signal queue.
    """
    rows = await pool.fetch(
        _TURNS_DUE_SQL,
        CLARIFY_NOTE_PREFIX + "%",
        AGENT_REPLY_PREFIX + "%",
        limit,
    )
    return [dict(r) for r in rows]


# --- operator sessions -------------------------------------------------------


async def upsert_operator_session(
    pool: Any,
    *,
    task_id: str,
    account: str,
    status: str,
    summary: str,
    session_id: str = "",
    host: str = "",
    problem_id: str = "",
) -> dict:
    """The operator's row on the task for `account`, written from what they
    reported. One live row per (task, account): a second report updates it,
    so a session that keeps reporting keeps one line in the registry rather
    than one per report. A `done` row is left as history and the next report
    from that account starts a new one.

    `session_id` is the Claude session's own id when the reporter knows it —
    that is what lets the sweep's `claude agents --json` cross-check see the
    row as live. When it does not, one is minted so the column stays non-null
    and the row still keys.
    """
    if status not in STATUSES:
        raise ValueError(f"unknown session status {status!r}")
    account = (account or "").strip() or "operator"
    sid = _uuid_or_none(session_id)
    pid = _uuid_or_none(problem_id)
    summary = (summary or "").strip()[:500]
    existing = await pool.fetchval(
        "SELECT id FROM work_sessions WHERE task_id = $1 AND owner = 'operator' "
        "AND account = $2 AND status <> 'done' ORDER BY created_at DESC LIMIT 1",
        task_id,
        account,
    )
    if existing is not None:
        await pool.execute(
            "UPDATE work_sessions SET status = $2, summary = $3, last_seen_at = now(), "
            "session_id = COALESCE($4, session_id), host = COALESCE(NULLIF($5, ''), host), "
            "problem_id = COALESCE($6, problem_id) WHERE id = $1",
            existing,
            status,
            summary,
            sid,
            host or "",
            pid,
        )
        row_id = existing
    else:
        row_id = await pool.fetchval(
            "INSERT INTO work_sessions (task_id, agent_id, session_id, problem_id, host, "
            "account, engine, owner, status, summary, last_seen_at) "
            "VALUES ($1, '', $2, $3, $4, $5, 'claude', 'operator', $6, $7, now()) RETURNING id",
            task_id,
            sid or uuid.uuid4(),
            pid,
            host or "",
            account,
            status,
            summary,
        )
    row = await pool.fetchrow(f"SELECT {_COLS} FROM work_sessions WHERE id = $1", row_id)
    return dict(row)


async def live_operator_sessions(
    pool: Any, task_id: str, *, within: timedelta = OPERATOR_LIVE_WINDOW
) -> list[dict]:
    """Operator rows on the task that are `active` and were seen inside
    `within` — the rows the collision check reads as "you are in it"."""
    rows = await pool.fetch(
        f"SELECT {_COLS} FROM work_sessions WHERE task_id = $1 AND owner = 'operator' "
        "AND status = 'active' AND last_seen_at > now() - $2::interval "
        "ORDER BY last_seen_at DESC",
        task_id,
        within,
    )
    return [dict(r) for r in rows]


async def reconcile_operator_sessions(
    pool: Any, live_session_ids: list[str], *, within: timedelta = OPERATOR_LIVE_WINDOW
) -> dict:
    """The liveness cross-check. An active operator row whose session id is in
    the coding host's inventory is touched; one that is not, and has not
    reported inside `within`, is parked. Rows the inventory cannot see (no
    session id reported) age out on the same clock — the collision check
    already ignores them past the window, so parking them only makes the
    registry say what the check already does.
    """
    ids = [s for s in (live_session_ids or []) if s]
    refreshed = await pool.execute(
        "UPDATE work_sessions SET last_seen_at = now() WHERE owner = 'operator' "
        "AND status = 'active' AND session_id::text = ANY($1::text[])",
        ids,
    )
    parked = await pool.execute(
        "UPDATE work_sessions SET status = 'parked' WHERE owner = 'operator' "
        "AND status = 'active' AND NOT (session_id::text = ANY($1::text[])) "
        "AND COALESCE(last_seen_at, created_at) < now() - $2::interval",
        ids,
        within,
    )
    return {"refreshed": _rows_affected(refreshed), "parked": _rows_affected(parked)}


# --- dispatch ----------------------------------------------------------------


async def dispatch_task_turn(
    client: Any,
    *,
    task_id: str,
    agent_id: str,
    comment: str,
    task_queue: str = "aegis-main",
) -> str:
    """Land `comment` on the task's single workflow. Returns "started"/"signalled".

    The workflow id is derived from the task, so a task has at most one run: if
    one is already going the comment is signalled into it, and if it finished
    between the two calls we start a fresh one.
    """
    wf_id = f"agent-task-{task_id}"
    payload = {"agent_id": agent_id, "todoist_task_id": task_id, "task": {}, "comment": comment}

    async def _start() -> None:
        await client.start_workflow("AgentTaskFlow", payload, id=wf_id, task_queue=task_queue)

    try:
        await _start()
        return "started"
    except WorkflowAlreadyStartedError:
        pass
    try:
        await client.get_workflow_handle(wf_id).signal("comment", comment)
        return "signalled"
    except Exception:  # noqa: BLE001 — the flow completed between the two calls
        await _start()
        return "started"
