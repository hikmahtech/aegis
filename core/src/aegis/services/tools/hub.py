"""Chat tools over the problem hub (`services/hub.py`, `services/work_sessions.py`).

Four tools, all on the operator MCP mount and in chat:

* `set_service_state` — declare a deploy / maintenance window.
* `task_context` — what a session should read first: the problem, its recent
  events, every session on it, and the take-over command.
* `report_progress` — the operator's session registering itself on the task,
  with a one-line summary; the projector turns it into a comment and a line
  in the task's status block.
* `merge_problems` — fold a duplicate problem into the one to keep.

Two are withheld from coding runs (`routes/mcp_server.py::_UNSERVED_TOOLS`):
`set_service_state`, because a run that could open a maintenance window could
silence the alert about itself, and `report_progress`, because an AEGIS turn
reports through its own activity and a run must not be able to mark its own
task done. `merge_problems` is withheld too — a merge hides a problem, and
that is a person's call.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

import asyncpg
import structlog

from aegis.services import hub_project, work_sessions
from aegis.services.hub import (
    Event,
    add_link,
    find_problem_for_task,
    get_problem,
    ingest_event,
    list_events,
    list_service_states,
    merge_problems,
    set_service_state,
)
from aegis.services.tools.base import ToolContext
from aegis.services.tools.registry import aegis_tool

logger = structlog.get_logger()

_EVENT_LIMIT = 20


def _fmt_until(row: dict) -> str:
    until = row.get("until_at")
    return f"until {until:%Y-%m-%d %H:%M} UTC" if until else "until cleared"


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(str(value or "").strip()))
    except ValueError:
        return ""


@aegis_tool
async def _exec_set_service_state(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    subject: str,
    state: Literal["deploying", "maintenance", "degraded", "ok"],
    minutes: int = 30,
    note: str = "",
) -> str:
    """Declare a swarm service or node deploying, in maintenance, degraded, or ok again. While a subject is deploying or in maintenance the problem hub records what it sees there but raises nothing; `ok` ends the window early.

    Args:
        subject: the swarm service (`stack_service`) or node name, or `*` for everything.
        state: deploying | maintenance | degraded | ok.
        minutes: how long the window lasts; ignored for ok.
        note: why — shown on every problem the window suppresses.
    """
    subject = (subject or "").strip()
    kind = "*" if subject == "*" else "service"
    try:
        row = await set_service_state(
            pool,
            subject,
            state,
            subject_kind=kind,
            minutes=minutes if state != "ok" else None,
            set_by=f"chat:{ctx.agent_id or 'unknown'}",
            note=note,
        )
    except ValueError as exc:
        return f"Refused: {exc}"
    if state == "ok":
        head = (
            f"{row['subject']}: window cleared."
            if row.get("cleared")
            else f"{row['subject']}: no window was set."
        )
    else:
        head = f"{row['subject']}: {row['state']} {_fmt_until(row)} (set by {row['set_by']})."
    active = await list_service_states(pool)
    if not active:
        return head + " No windows in force."
    lines = [f"- {r['subject']} ({r['subject_kind']}): {r['state']} {_fmt_until(r)}" for r in active]
    return head + " Windows in force:\n" + "\n".join(lines)


def _event_line(e: dict[str, Any]) -> str:
    payload = e.get("payload") or {}
    text = str(
        payload.get("text")
        or payload.get("summary")
        or payload.get("reason")
        or payload.get("title")
        or payload.get("action")
        or ""
    ).strip()
    when = hub_project._ts(e.get("occurred_at"))
    head = f"- {when} {e.get('kind')}/{e.get('source')}"
    return f"{head}: {text[:200]}" if text else head


async def _task_row(pool: asyncpg.Pool, task_id: str) -> dict[str, Any] | None:
    row = await pool.fetchrow(
        "SELECT id, content, labels, is_completed FROM todoist_tasks WHERE id = $1", task_id
    )
    return dict(row) if row else None


def _take_over(sess: dict[str, Any]) -> str:
    """The command that resumes AEGIS's session in its own worktree."""
    if sess.get("owner") != "aegis" or not sess.get("session_id"):
        return ""
    env = f"CLAUDE_CONFIG_DIR=<{sess['account']}> " if sess.get("account") else ""
    return f"cd {sess.get('worktree_path') or '<worktree>'} && {env}claude --resume {sess['session_id']}"


@aegis_tool
async def _exec_task_context(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    task_id: str = "",
    problem_id: str = "",
) -> str:
    """What to read first when picking up a task: the problem behind it, its recent events, every session on it (AEGIS's and yours) with their summaries, its links and any deploy window, plus the command that takes AEGIS's session over. Give the Todoist task id or the problem id.

    Args:
        task_id: the Todoist task id (the number in the task's URL, or `task-<id>` in a worktree path).
        problem_id: the problem's uuid, when you have it instead of the task.
    """
    task_id = (task_id or "").strip()
    pid = _uuid(problem_id)
    problem = await get_problem(pool, pid) if pid else None
    if problem is None and task_id:
        problem = await find_problem_for_task(pool, task_id)
    if problem is None and not task_id:
        return "Refused: task_id or problem_id is required" + (
            f" (no problem {problem_id})" if problem_id else ""
        )
    if problem is not None and not task_id:
        task_id = str(problem.get("todoist_task_id") or "")

    lines: list[str] = []
    task = await _task_row(pool, task_id) if task_id else None
    if task is not None:
        done = " (completed)" if task.get("is_completed") else ""
        labels = " ".join(task.get("labels") or [])
        lines.append(f"Task {task['id']}: {task['content']}{done}" + (f" [{labels}]" if labels else ""))
    elif task_id:
        lines.append(f"Task {task_id}: not in the mirror")

    if problem is None:
        lines.append("No problem on the hub for this task (a plain @code task).")
    else:
        lines.append(f"Problem {problem['id']}: {problem['title']}")
        window = None
        async with pool.acquire() as conn:
            from aegis.services.hub import _active_suppression, _utcnow

            window = await _active_suppression(
                conn, problem["subject"], problem["subject_kind"], _utcnow()
            )
        links = [
            dict(r)
            for r in await pool.fetch(
                "SELECT link_kind, ref FROM problem_links WHERE problem_id = $1::uuid "
                "ORDER BY created_at",
                problem["id"],
            )
        ]
        block = hub_project.render_block(
            problem, window=dict(window) if window else None, links=links
        )
        lines.extend(block.splitlines()[1:-1])

    sessions = await work_sessions.list_for_task(pool, task_id) if task_id else []
    if sessions:
        lines.append("Sessions:")
        for sess in sessions:
            line = "- " + hub_project.session_line(sess)
            cmd = _take_over(sess)
            if cmd:
                line += f"\n  take over: {cmd}"
            lines.append(line)
    else:
        lines.append("Sessions: none registered")

    if problem is not None:
        events = await list_events(pool, problem["id"], limit=_EVENT_LIMIT)
        if events:
            lines.append(f"Recent events (newest first, {len(events)}):")
            lines.extend(_event_line(e) for e in events)
    return "\n".join(lines)


@aegis_tool
async def _exec_report_progress(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    task_id: str,
    summary: str,
    status: Literal["active", "parked", "done"] = "active",
    session_id: str = "",
    pr_url: str = "",
    account: str = "",
) -> str:
    """Register your own session on a task with a one-line summary of where you are. It lands as a comment on the task and a line in its status block, and while your session is `active` AEGIS stays out of the task. A task with no problem on the hub gets one. Call it when you start, when you hand back, and when you are done.

    Args:
        task_id: the Todoist task id.
        summary: one line: what you did or where you stopped.
        status: active (you are on it) | parked (stepped away, AEGIS may resume) | done.
        session_id: your Claude session id, if you know it — lets the host's session list confirm you are live.
        pr_url: a pull request you opened, linked to the problem.
        account: the CLAUDE_CONFIG_DIR account label you run under, if not the default.
    """
    task_id = (task_id or "").strip()
    summary = (summary or "").strip()
    if not task_id or not summary:
        return "Refused: task_id and summary are required"
    task = await _task_row(pool, task_id)
    if task is None:
        return f"Refused: task {task_id} is not in the Todoist mirror"

    problem = await find_problem_for_task(pool, task_id)
    created = False
    if problem is None:
        # A plain @code task: give it a problem so the registry and the
        # timeline work for every task, not only alert-born ones.
        aegis_row = await work_sessions.get_session(pool, task_id)
        subject = str((aegis_row or {}).get("github_repo") or "") or f"task-{task_id}"
        try:
            result = await ingest_event(
                pool,
                Event(
                    source="session",
                    external_id=f"task-{task_id}",
                    kind="occurrence",
                    title=str(task["content"] or f"Task {task_id}")[:200],
                    subject=subject,
                    subject_kind="repo",
                    klass="manual",
                    severity="info",
                    payload={"task_id": task_id},
                ),
            )
        except ValueError as exc:
            return f"Refused: {exc}"
        if not result.problem_id:
            return "Refused: the hub did not record a problem for this task"
        await hub_project.link_task(pool, result.problem_id, task_id)
        problem = await get_problem(pool, result.problem_id)
        created = True
    assert problem is not None
    pid = problem["id"]

    account = (account or "").strip() or "operator"
    row = await work_sessions.upsert_operator_session(
        pool,
        task_id=task_id,
        account=account,
        status=status,
        summary=summary,
        session_id=session_id,
        problem_id=pid,
    )
    linked = False
    if pr_url.strip():
        linked = await add_link(pool, pid, "github_pr", pr_url)
    try:
        await ingest_event(
            pool,
            Event(
                source="session",
                external_id=f"session:{row['id']}:{uuid.uuid4().hex[:12]}",
                kind="session_note",
                title=problem["title"],
                severity="info",
                problem_id=pid,
                payload={
                    "text": f"{status} ({account}): {summary}",
                    "status": status,
                    "account": account,
                    "pr_url": pr_url.strip(),
                    "session_id": row.get("session_id") or "",
                },
            ),
        )
    except ValueError as exc:
        return f"Refused: {exc}"
    # Project now rather than at the next sweep, so the comment is on the task
    # while the session that wrote it is still there. Best-effort: the sweep
    # re-derives whatever this could not post.
    try:
        await hub_project.project(pool, pid, settings=ctx.settings)
    except Exception as exc:  # noqa: BLE001
        logger.warning("report_progress_project_failed", problem_id=pid, error=str(exc)[:200])

    head = f"Recorded on task {task_id}: {status} ({account}) — {summary[:120]}"
    if created:
        head += f"\nCreated problem {pid} for it."
    if linked:
        head += f"\nLinked {pr_url.strip()}."
    aegis_row = next((s for s in await work_sessions.list_for_task(pool, task_id) if s["owner"] == "aegis"), None)
    if aegis_row is not None:
        head += f"\nAEGIS's session: {hub_project.session_line(aegis_row)}"
    return head


@aegis_tool
async def _exec_merge_problems(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    keep_id: str,
    merge_id: str,
) -> str:
    """Fold one problem into another when they are the same outage under two names: events, links and sessions move to the kept problem, the merged one closes with a link back, and its task is completed with a note. Only do this when you are sure — a wrong merge hides an outage.

    Args:
        keep_id: the problem to keep (uuid).
        merge_id: the duplicate to fold into it (uuid).
    """
    keep = _uuid(keep_id)
    merge = _uuid(merge_id)
    if not keep or not merge:
        return "Refused: keep_id and merge_id must be problem uuids"
    try:
        result = await merge_problems(pool, keep, merge, by=f"chat:{ctx.agent_id or 'unknown'}")
    except ValueError as exc:
        return f"Refused: {exc}"
    kept = await get_problem(pool, keep)
    lines = [
        f"Merged {merge} into {keep}: {result['events_moved']} events moved; "
        f"the kept problem is now {kept['status'] if kept else 'unknown'} with "
        f"{kept['occurrences'] if kept else '?'} occurrences."
    ]
    merged_task = result.get("merged_task_id") or ""
    keep_task = str((kept or {}).get("todoist_task_id") or "")
    if merged_task and merged_task != keep_task:
        retired = await hub_project.retire_task(
            pool,
            merged_task,
            f"Merged into problem {keep}" + (f" (task {keep_task})" if keep_task else "") + ".",
            settings=ctx.settings,
        )
        lines.append(
            f"Task {merged_task} {'completed' if retired else 'could not be completed'} with a note."
        )
    return "\n".join(lines)
