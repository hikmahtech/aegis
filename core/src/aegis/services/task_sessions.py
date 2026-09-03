"""task_sessions — one persistent coding session per @code Todoist task.

The comment thread on the task is the control channel; this module owns the row
that lets a later comment resume the same session in the same worktree, and the
start-or-signal dispatch that lands every comment on the task's single workflow.

Two things are deliberate here:

* ``session_id`` is minted once, by ``create_session``'s ``ON CONFLICT DO
  NOTHING``. A second caller for the same task gets the *existing* row back, so
  two comments arriving together can never fork a task into two sessions.
* ``find_turns_due`` compares the newest **user** note against the row's own
  watermark. AEGIS's own notes are excluded in SQL rather than in Python for the
  same reason ClarifyFlow does it (see ``aegis.clarify_note``): a machine note
  that counted as a turn would make the task answer itself forever.
"""

from __future__ import annotations

import uuid
from typing import Any

from temporalio.exceptions import WorkflowAlreadyStartedError

from aegis.clarify_note import AGENT_REPLY_PREFIX, CLARIFY_NOTE_PREFIX

# session_id is a uuid column; every consumer wants the string form.
_COLS = (
    "task_id, agent_id, session_id::text AS session_id, repo, github_repo, "
    "worktree_path, branch, host, slack_ref, turns, last_turn_at, created_at"
)

# The newest user note on a live task, when it is newer than the last turn we
# ran for it. `created_at` is the watermark until the first turn, so the comment
# that created the session does not immediately re-fire it.
_TURNS_DUE_SQL = """
SELECT ts.task_id, ts.agent_id, n.content AS comment
FROM task_sessions ts
JOIN todoist_tasks t ON t.id = ts.task_id AND NOT t.is_completed
JOIN LATERAL (
    SELECT content, posted_at FROM todoist_notes
    WHERE item_id = ts.task_id
      AND content NOT LIKE $1 AND content NOT LIKE $2
      AND content NOT LIKE '%Workflow run:%'
    ORDER BY posted_at DESC LIMIT 1
) n ON n.posted_at > COALESCE(ts.last_turn_at, ts.created_at)
ORDER BY n.posted_at ASC
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


async def get_session(pool: Any, task_id: str) -> dict | None:
    row = await pool.fetchrow(f"SELECT {_COLS} FROM task_sessions WHERE task_id = $1", task_id)
    return dict(row) if row else None


async def create_session(pool: Any, *, task_id: str, agent_id: str) -> dict:
    """The task's session row, creating it (with a fresh session id) if absent.

    Idempotent: a task already holding a session keeps its `session_id`, so a
    concurrent second caller never mints a competing session.
    """
    await pool.execute(
        "INSERT INTO task_sessions (task_id, agent_id, session_id) VALUES ($1, $2, $3) "
        "ON CONFLICT (task_id) DO NOTHING",
        task_id,
        agent_id,
        uuid.uuid4(),
    )
    row = await get_session(pool, task_id)
    if row is None:  # pragma: no cover — the insert above guarantees a row
        raise RuntimeError(f"task_sessions row vanished for task {task_id}")
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
        "UPDATE task_sessions SET repo = $2, github_repo = $3, worktree_path = $4, "
        "branch = $5, host = $6 WHERE task_id = $1",
        task_id,
        repo,
        github_repo,
        worktree_path,
        branch,
        host,
    )


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
        "UPDATE task_sessions SET last_turn_at = now(), turns = turns + $2 WHERE task_id = $1",
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
        "UPDATE task_sessions SET slack_ref = $2 WHERE task_id = $1", task_id, ref
    )


async def find_by_thread(pool: Any, channel: str, ts: str) -> str | None:
    """The task whose session owns this Slack thread root, or None."""
    row = await pool.fetchrow(
        "SELECT task_id FROM task_sessions "
        "WHERE slack_ref->>'channel' = $1 AND slack_ref->>'ts' = $2",
        channel,
        ts,
    )
    return row["task_id"] if row else None


async def find_turns_due(pool: Any, limit: int = 20) -> list[dict]:
    """Sessions with an unanswered user comment: `[{task_id, agent_id, comment}]`."""
    rows = await pool.fetch(
        _TURNS_DUE_SQL,
        CLARIFY_NOTE_PREFIX + "%",
        AGENT_REPLY_PREFIX + "%",
        limit,
    )
    return [dict(r) for r in rows]


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
