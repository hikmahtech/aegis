"""GTD / Todoist chat tools — capture, next actions, and task state changes.

Every write here goes through the Todoist Sync API and therefore through
`_stage_chat_tool_outbox`, which decides whether a failed batch is queued for
retry or dropped. The read tools query the local `todoist_tasks` /
`todoist_projects` projection, never the API.

`_capture_to_inbox_impl` is the shared capture core: `services/chat.py`
re-exports it, and `api/routes/chat.py` + `api/routes/capture.py` import it
from there.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

import asyncpg
import structlog

from aegis.services.todoist_config import resolve_todoist_api_key
from aegis.services.tools.base import ToolContext
from aegis.services.tools.registry import aegis_tool

logger = structlog.get_logger()


async def _capture_to_inbox_impl(
    pool,
    source_tag: str,
    external_id: str,
    title: str,
    description: str | None,
    extra_labels: list[str] | None = None,
) -> str | None:
    """Thin wrapper that lets tests monkeypatch the capture core.

    In production this delegates to the same logic as
    CaptureActivities.capture_to_inbox; we keep the HTTP-facing service
    layer decoupled from the worker activity module so chat-tool calls
    don't pull worker imports into Core.

    `extra_labels` are appended to the `[source_tag]` label set (dedup-
    preserving) — used to assign a captured task to an agent (e.g.
    `@pandora`) so it anchors that agent's downstream workflows.
    """
    from aegis.connectors.todoist import TodoistConnector

    if pool is None:
        return None
    async with pool.acquire() as conn:
        kill = await conn.fetchval(
            "SELECT value FROM settings WHERE key = 'todoist_capture_enabled'"
        )
        if kill is False or (isinstance(kill, dict) and kill.get("value") is False):
            return None
        managed = await conn.fetchval(
            "SELECT value FROM settings WHERE key = 'todoist_managed_project_ids'"
        )
        inbox_id = (managed or {}).get("inbox") if isinstance(managed, dict) else None
        if not inbox_id:
            return None
        inserted = await conn.fetchval(
            "INSERT INTO todoist_capture_idempotency (source_tag, external_id) "
            "VALUES ($1,$2) ON CONFLICT DO NOTHING RETURNING captured_at",
            source_tag,
            external_id,
        )
        if inserted is None:
            existing = await conn.fetchval(
                "SELECT todoist_task_ref FROM todoist_capture_idempotency "
                "WHERE source_tag=$1 AND external_id=$2",
                source_tag,
                external_id,
            )
            return existing

    from aegis.config import Settings

    settings = Settings()
    _tk = await resolve_todoist_api_key(pool, settings)
    if not _tk:
        return None
    connector = TodoistConnector(api_key=_tk, db_pool=pool, timeout=10.0)
    item_labels = [source_tag]
    for lbl in extra_labels or []:
        if lbl and lbl not in item_labels:
            item_labels.append(lbl)
    cmd = TodoistConnector.build_create_item_command(
        project_id=inbox_id,
        content=title[:120],
        description=description,
        labels=item_labels,
    )
    result = await connector.commands([cmd])
    status = TodoistConnector.check_sync_status(result, [cmd["uuid"]])
    ref: str | None = None
    if status["ok"]:
        mapping = (result.get("data") or {}).get("temp_id_mapping", {}) or {}
        ref = mapping.get(cmd["temp_id"])
    elif status["retryable"] or status["rejected_retryable"]:
        # Transient failure — queue for drain_outbox to retry.
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO todoist_outbox (temp_id, command, status) "
                "VALUES ($1,$2,'pending') ON CONFLICT (temp_id) DO NOTHING",
                cmd["temp_id"],
                cmd,
            )
        ref = cmd["temp_id"]
    # Permanent rejection (ITEM_NOT_FOUND / INVALID_ARGUMENT etc.) leaves
    # ref=None so the idempotency row keeps todoist_task_ref NULL — the
    # caller surfaces "no ref" to the user instead of poisoning the outbox.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE todoist_capture_idempotency SET todoist_task_ref=$1 "
            "WHERE source_tag=$2 AND external_id=$3",
            ref,
            source_tag,
            external_id,
        )
    return ref


async def _stage_chat_tool_outbox(
    pool: asyncpg.Pool | None,
    commands: list[dict],
    status: dict,
    op: str,
) -> str | None:
    """Inspect a `check_sync_status()` envelope for a chat-tool command batch.

    Three outcomes:
    - Status OK → returns None; caller proceeds to its success path.
    - Failure is retryable (envelope 5xx-class OR per-cmd transient rejection)
      → stage each command in `todoist_outbox` and return a user-facing
      "queued for retry" string so the user can stop waiting on the chat
      reply.
    - Failure is permanent (envelope 4xx OR per-cmd ITEM_NOT_FOUND etc.)
      → return a user-facing "Todoist error" string. No outbox stage —
      replaying a malformed command just burns retries.

    Matches the outbox-queue contract that `_capture_to_inbox_impl`,
    `CaptureActivities.capture_to_inbox`, and `ClarifyActivities.apply_outcome`
    already use, so transient Todoist outages don't silently drop user
    intent across any code path.
    """
    if status["ok"]:
        return None
    if status["retryable"] or status["rejected_retryable"]:
        if pool is None:
            return f"Todoist transient error ({op}); no pool to queue retry"
        import uuid as _uuid

        async with pool.acquire() as conn:
            for cmd in commands:
                temp_id = cmd.get("temp_id") or f"chattool-{op}-{_uuid.uuid4()}"
                await conn.execute(
                    "INSERT INTO todoist_outbox (temp_id, command, status) "
                    "VALUES ($1, $2, 'pending') ON CONFLICT (temp_id) DO NOTHING",
                    temp_id,
                    cmd,
                )
        logger.warning(
            "chat_tool_outbox_queued",
            op=op,
            count=len(commands),
            envelope_error=status["envelope_error"],
        )
        return f"Todoist hiccup ({op}); queued for retry"
    return f"Todoist error ({op}): {status['envelope_error'] or status['rejected']}"


async def _assignee_labels(pool: asyncpg.Pool | None) -> list[str]:
    """Valid handoff assignee labels: @me plus every active agent's mention
    aliases (metadata.mention_aliases, default [id]) — issue #36. Falls back to
    the shipped 4-agent set without a pool or on read failure."""
    fallback = ["@me", "@sebas", "@raphael", "@maou", "@pandora"]
    if pool is None:
        return fallback
    try:
        rows = await pool.fetch("SELECT id, metadata FROM agents WHERE active = TRUE")
        labels = ["@me"]
        for r in rows:
            aliases = (r["metadata"] or {}).get("mention_aliases") or [r["id"]]
            labels.extend(f"@{str(a).lstrip('@')}" for a in aliases)
        return labels or fallback
    except Exception as exc:  # noqa: BLE001 — never break the tool on a config read
        logger.warning("handoff_assignee_labels_failed", error=str(exc)[:200])
        return fallback


async def _user_today(pool: asyncpg.Pool | None) -> date:
    """Today's calendar date in the user's own timezone (`settings.user_timezone`).

    Deliberately NOT `CURRENT_DATE`. The Postgres session runs in UTC while
    `todoist_tasks.due_date` is a plain `date` holding the user's LOCAL calendar
    date, so UTC-today is the user's YESTERDAY for the whole 00:00–05:30 IST
    window — plausible chat hours, and exactly when someone asks what is still
    due today. (`aegis_worker.activities.review` gets away with a bare
    `CURRENT_DATE` only because those flows run on a morning schedule; chat is
    called at any hour.)

    Never raises. A missing pool, a missing row, a non-string value, an unknown
    zone or a failed read all fall back to UTC — a typo'd setting must not take
    a chat tool down. The pool's jsonb codec (`db/pool.py`) json-decodes the
    stored scalar for us, so `"Asia/Kolkata"` arrives as the bare zone name.
    """
    tz = ZoneInfo("UTC")
    if pool is not None:
        try:
            row = await pool.fetchrow(
                "SELECT value FROM settings WHERE key = $1", "user_timezone"
            )
            name = row["value"] if row else None
            if isinstance(name, str) and name.strip():
                tz = ZoneInfo(name.strip())
        except Exception as exc:  # noqa: BLE001 — never break the tool on a config read
            logger.warning("user_timezone_read_failed", error=str(exc)[:200])
    return datetime.now(tz).date()


@aegis_tool
async def _exec_capture_to_inbox(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    text: str,
    source: Literal["chat", "manual"] = "chat",
    description: str | None = None,
) -> str:
    """Drop a task into the Todoist Inbox. The task gets a #chat source tag by default unless 'source' is given.

    Args:
        text: Task title
        source: Where the capture originated (tags as #<source>)
        description: Optional longer body
    """
    text = (text or "").strip()
    if not text:
        return "Refused: empty text"
    source = source or "chat"
    # Deterministic external id from (agent, text) so identical re-asks
    # dedupe; including agent_id keeps separate personalities independent.
    import hashlib

    agent = (ctx.agent_id if ctx else None) or "chat"
    ext_id = f"chat:{agent}:{hashlib.sha256(text.encode()).hexdigest()[:16]}"
    ref = await _capture_to_inbox_impl(
        pool=pool,
        source_tag=f"#{source}",
        external_id=ext_id,
        title=text,
        description=description,
    )
    if ref is None:
        return "Capture skipped (kill switch off, missing inbox, or no api key)"
    return f"Captured: {ref}"


@aegis_tool
async def _exec_list_next_actions(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    assignee: str | None = None,
    context: str | None = None,
    due: Literal["today", "this_week", "overdue"] | None = None,
    limit: int = 25,
) -> str:
    """Read open (incomplete), actionable tasks from the Todoist projection.
    Excludes @reference/@someday/@to-read, and excludes @waiting for @me. When
    assignee is an agent label (e.g. @pandora), @waiting tasks ARE included and
    marked [parked] — for an agent @waiting means 'a run finished a pass', not
    'blocked', so this is that agent's own working queue. Optional filters:
    assignee label, context label, due window.

    Args:
        assignee: Assignee label (e.g. @me, @sebas)
        context: Context label (e.g. @5min, @deep)
        due: Due window. overdue=past due; today=due today or overdue;
            this_week=due within 7 days, today, or overdue. Undated tasks
            excluded.
        limit: Max rows to return
    """
    limit = int(limit or 25)
    if pool is None:
        return "No DB pool"
    # State labels mirror aegis_worker.activities.review._STATE_LABELS
    # (cross-package; keep in sync) and _exec_whats_next below — a task parked
    # as @waiting/@reference/@someday/@to-read isn't a human next action.
    parked = ["@waiting", "@reference", "@to-read", "@someday"]
    # ...but @waiting on an AGENT-assigned task is not "blocked": it is
    # agent_task.PARK_LABEL, stamped by park_task at the END of every run. So
    # filtering it hid the agent's entire worked backlog from the agent itself
    # (9 of 11 open @pandora tasks, 2026-08-11) and chat truthfully reported an
    # empty queue. Agents see their parked work; @me keeps GTD semantics.
    # Roster comes from the DB, so a new agent is covered without a code edit.
    is_agent = bool(assignee) and assignee != "@me" and assignee in await _assignee_labels(pool)
    if is_agent:
        parked.remove("@waiting")
    params: list[object] = [parked]
    where = [
        "NOT t.is_completed",
        "NOT (t.labels && $1::text[])",
    ]
    if assignee:
        params.append(assignee)
        where.append(f"t.assignee_label = ${len(params)}")
    if context:
        params.append(context)
        where.append(f"${len(params)} = ANY(t.labels)")
    # The due window. `due` was advertised to the LLM from Phase 3 but never
    # read (#324), so "what's due today" silently answered with the UNFILTERED
    # list — a confident wrong answer, not a missing feature.
    #
    # The windows are NESTED, not disjoint: `today` and `this_week` include what
    # is already overdue. That matches Todoist's own Today view (what the user
    # actually looks at) and this repo's existing meaning — review.py:109 names a
    # `due_date <= CURRENT_DATE` query `due_today_count`, and :448 uses
    # `< CURRENT_DATE` for overdue.
    #
    # All three require a due_date. A due window is a question about dates, and
    # an undated @next task is a first-class GTD state (deliberately dateless),
    # not "due always" — it has no position in any window. Undated work stays
    # reachable through the no-filter call and `whats_next`.
    #
    # One reference date, computed in the USER's timezone (see `_user_today` —
    # CURRENT_DATE would be wrong here) and bound as a parameter. `::date` on
    # the `this_week` parameter is required, not decoration: without it Postgres
    # infers the parameter's type from the `+ 7` and the query dies with
    # "operator does not exist: date <= integer". The cast is on the PARAMETER,
    # so the planner still folds the whole expression to a constant and every
    # predicate stays sargable against the partial index
    # `todoist_tasks_due_date_idx (due_date) WHERE NOT is_completed`.
    due_sql = {
        "overdue": "t.due_date IS NOT NULL AND t.due_date < ${n}",
        "today": "t.due_date IS NOT NULL AND t.due_date <= ${n}",
        "this_week": "t.due_date IS NOT NULL AND t.due_date <= ${n}::date + 7",
    }.get(due or "")
    if due_sql:
        params.append(await _user_today(pool))
        where.append(due_sql.format(n=len(params)))
    async with pool.acquire() as conn:
        inbox_id = await conn.fetchval(
            "SELECT value->>'inbox' FROM settings WHERE key='todoist_managed_project_ids'"
        )
        # The Inbox is excluded for humans because an Inbox item is unclarified
        # — clarify it before it can be a next action. An agent-assigned task is
        # the opposite: the @agent label IS clarify's output, and AEGIS's own
        # triage (#alert/#email/#receipt) parks its work there, so 10 of the 11
        # open @pandora tasks were Inbox rows. agent_task.find_actionable_tasks
        # already works them with no inbox filter, so excluding them here only
        # ever hid work the worker was actively doing.
        if inbox_id and not is_agent:
            params.append(inbox_id)
            where.append(f"t.project_id <> ${len(params)}")
        params.append(limit)
        sql = (
            "SELECT t.id, t.content, t.assignee_label, t.labels, t.due_date "
            "FROM todoist_tasks t "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY COALESCE(t.due_date,'9999-12-31'::date), t.updated_at DESC "
            f"LIMIT ${len(params)}"
        )
        rows = await conn.fetch(sql, *params)
        if not rows:
            # An empty list used to be indistinguishable from "everything was
            # filtered out", which is exactly how a full queue got reported as
            # nothing-to-do. Say what was hidden so the agent can't confabulate.
            hidden = await conn.fetchval(
                "SELECT count(*) FROM todoist_tasks t WHERE NOT t.is_completed "
                "AND t.labels && $1::text[] "
                "AND ($2::text IS NULL OR t.assignee_label = $2)",
                parked,
                assignee,
            )
            if hidden:
                return (
                    f"No matching next actions ({hidden} excluded as "
                    f"{'/'.join(parked)})."
                )
            return "No matching next actions."
    lines = []
    for r in rows:
        due_note = f" due {r['due_date'].isoformat()}" if r["due_date"] else ""
        parked_note = " [parked]" if "@waiting" in (r["labels"] or []) else ""
        lines.append(
            f"- [{r['id']}] {r['content']} ({r['assignee_label'] or '@me'}){due_note}{parked_note}"
        )
    return "\n".join(lines)


@aegis_tool(empty_required=True)
async def _exec_whats_next(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    minutes: int | None = None,
    energy: Literal["low", "high"] | None = None,
    limit: int | None = None,
) -> str:
    """Suggest what to work on now. Returns a short ranked list of your own
    next actions (excludes waiting/reference/reading and inbox/someday).
    Optionally tailor to available time and energy.

    Args:
        minutes: Minutes available (<=5 prefers @5min tasks)
        energy: low prefers light tasks; high prefers deep work
        limit: Max items (default 5)
    """
    if pool is None:
        return "No DB pool"
    energy = (energy or "").lower()
    # ponytail: tiny inline minutes/energy->context map; not worth a module.
    contexts: list[str] = []
    if minutes is not None and int(minutes) <= 5:
        contexts = ["@5min"]
    elif energy == "low":
        contexts = ["@5min", "@email", "@reading"]
    elif energy == "high":
        contexts = ["@deep", "@code"]
    where = [
        "NOT t.is_completed",
        "(t.assignee_label='@me' OR t.assignee_label IS NULL)",
        # State labels mirror aegis_worker.activities.review._STATE_LABELS
        # (cross-package; keep in sync). @someday is included here now that
        # Someday/Later is a label, not a managed project (Todoist
        # restructure, 2026-07).
        "NOT (t.labels && ARRAY['@waiting','@reference','@to-read','@someday'])",
    ]
    params: list = []
    async with pool.acquire() as conn:
        managed = await conn.fetchval(
            "SELECT value FROM settings WHERE key='todoist_managed_project_ids'"
        )
        # Someday is excluded via the @someday state label above; only Inbox
        # is still a managed-project id to exclude.
        exclude = []
        if isinstance(managed, dict):
            exclude = [e for e in (managed.get("inbox"),) if e]
        if exclude:
            params.append(exclude)
            where.append(
                f"(t.project_id IS NULL OR t.project_id <> ALL(${len(params)}::text[]))"
            )
        if contexts:
            params.append(contexts)
            where.append(f"t.labels && ${len(params)}::text[]")
        params.append(int(limit or 5))
        sql = (
            "SELECT t.id, t.content, t.due_date FROM todoist_tasks t "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY (t.due_date IS NULL), t.due_date ASC, "
            "t.priority DESC NULLS LAST, t.updated_at DESC "
            f"LIMIT ${len(params)}"
        )
        rows = await conn.fetch(sql, *params)
    if not rows:
        return "Nothing queued that fits — inbox may be clear or everything's @waiting."
    lines = []
    for r in rows:
        due = f" (due {r['due_date'].isoformat()})" if r["due_date"] else ""
        lines.append(f"- [{r['id']}] {r['content']}{due}")
    return "\n".join(lines)


@aegis_tool
async def _exec_list_projects(pool: asyncpg.Pool, ctx: ToolContext) -> str:
    """List work-stream projects with open task counts.

    Lists LEAF work-stream projects (nested under an area project) with
    open-task counts.

    Post-restructure, areas/work-streams are real nested Todoist projects
    (parent AREA project has parent_id IS NULL, leaf WORK-STREAM has
    parent_id IS NOT NULL) — the old `project/*` label convention is
    retired.
    """
    if pool is None:
        return "No DB pool"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT p.id, p.name, "
            "  count(t.id) FILTER (WHERE NOT t.is_completed) AS open_n "
            "FROM todoist_projects p "
            "LEFT JOIN todoist_tasks t ON t.project_id = p.id "
            "WHERE p.parent_id IS NOT NULL AND NOT p.is_archived "
            "GROUP BY p.id, p.name "
            "ORDER BY p.name"
        )
    if not rows:
        return "No work-stream projects."
    return "\n".join(f"- [{r['id']}] {r['name']} ({r['open_n']} open)" for r in rows)


@aegis_tool
async def _exec_complete_task(
    pool: asyncpg.Pool, ctx: ToolContext, *, task_id: str, note: str | None = None
) -> str:
    """Mark a Todoist task complete. Optional 'note' is appended as a Todoist comment."""
    import uuid as _uuid

    from aegis.config import Settings
    from aegis.connectors.todoist import TodoistConnector

    task_id = (task_id or "").strip()
    note_text = note
    if not task_id:
        return "Refused: task_id required"
    settings = Settings()
    _tk = await resolve_todoist_api_key(pool, settings)
    if not _tk:
        return "Todoist not configured"
    connector = TodoistConnector(api_key=_tk, db_pool=pool, timeout=10.0)
    commands = [
        {"type": "item_complete", "uuid": str(_uuid.uuid4()), "args": {"id": task_id}},
    ]
    if note_text:
        commands.append(TodoistConnector.build_note_add_command(task_id, note_text))
    result = await connector.commands(commands)
    status = TodoistConnector.check_sync_status(result, [c["uuid"] for c in commands])
    fail_msg = await _stage_chat_tool_outbox(pool, commands, status, "complete_task")
    if fail_msg is not None:
        return fail_msg
    return f"Completed {task_id}"


@aegis_tool
async def _exec_defer_task(
    pool: asyncpg.Pool, ctx: ToolContext, *, task_id: str, until: str
) -> str:
    """Reschedule a Todoist task to a new due date.

    Args:
        until: ISO date or natural string like 'tomorrow', 'next friday'
    """
    from aegis.config import Settings
    from aegis.connectors.todoist import TodoistConnector

    task_id = (task_id or "").strip()
    until = (until or "").strip()
    if not task_id or not until:
        return "Refused: task_id and until required"
    settings = Settings()
    _tk = await resolve_todoist_api_key(pool, settings)
    if not _tk:
        return "Todoist not configured"
    connector = TodoistConnector(api_key=_tk, db_pool=pool, timeout=10.0)
    # Todoist accepts natural-language strings under args.due.string
    cmd = TodoistConnector.build_item_update_command(task_id, due={"string": until})
    result = await connector.commands([cmd])
    status = TodoistConnector.check_sync_status(result, [cmd["uuid"]])
    fail_msg = await _stage_chat_tool_outbox(pool, [cmd], status, "defer_task")
    if fail_msg is not None:
        return fail_msg
    return f"Deferred {task_id} until {until}"


@aegis_tool
async def _exec_mark_waiting(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    task_id: str,
    who: str,
    expected_by: str | None = None,
) -> str:
    """Mark a task @waiting (with a 'who' note). Optionally include expected_by ISO date.

    Args:
        who: The person we're waiting on
        expected_by: Optional ISO date
    """
    from aegis.config import Settings
    from aegis.connectors.todoist import TodoistConnector

    task_id = (task_id or "").strip()
    who = (who or "").strip()
    expected = expected_by
    if not task_id or not who:
        return "Refused: task_id and who required"
    if pool is None:
        return "No DB pool"
    async with pool.acquire() as conn:
        existing_labels = await conn.fetchval(
            "SELECT labels FROM todoist_tasks WHERE id=$1", task_id
        )
    if existing_labels is None:
        return f"Unknown task {task_id}"
    settings = Settings()
    _tk = await resolve_todoist_api_key(pool, settings)
    if not _tk:
        return "Todoist not configured"
    connector = TodoistConnector(api_key=_tk, db_pool=pool, timeout=10.0)
    new_labels = list({*(existing_labels or []), "@waiting"})
    note_body = f"Waiting on {who}" + (f" (expected by {expected})" if expected else "")
    commands = [
        TodoistConnector.build_item_update_command(task_id, labels=new_labels),
        TodoistConnector.build_note_add_command(task_id, note_body),
    ]
    result = await connector.commands(commands)
    status = TodoistConnector.check_sync_status(result, [c["uuid"] for c in commands])
    fail_msg = await _stage_chat_tool_outbox(pool, commands, status, "mark_waiting")
    if fail_msg is not None:
        return fail_msg
    return f"Marked {task_id} waiting on {who}"


@aegis_tool
async def _exec_handoff_task(
    pool: asyncpg.Pool, ctx: ToolContext, *, task_id: str, to_assignee: str
) -> str:
    """Reassign a task to a different personality assignee, given as an @label
    (e.g. @me, @raphael, @pandora). Valid labels are the active agents' mention
    aliases plus @me; an invalid one is rejected with the list of valid labels.
    """
    from aegis.config import Settings
    from aegis.connectors.todoist import TodoistConnector

    task_id = (task_id or "").strip()
    to_assignee = (to_assignee or "").strip()
    if not task_id:
        return "Refused: valid task_id + to_assignee required"
    valid_assignees = await _assignee_labels(pool)
    if to_assignee not in valid_assignees:
        return f"Refused: to_assignee must be one of {', '.join(valid_assignees)}"
    if pool is None:
        return "No DB pool"
    async with pool.acquire() as conn:
        existing_labels = await conn.fetchval(
            "SELECT labels FROM todoist_tasks WHERE id=$1", task_id
        )
    if existing_labels is None:
        return f"Unknown task {task_id}"
    # Strip any existing @assignee, add the new one
    kept = [lab for lab in (existing_labels or []) if lab not in valid_assignees]
    new_labels = [*kept, to_assignee]
    settings = Settings()
    _tk = await resolve_todoist_api_key(pool, settings)
    if not _tk:
        return "Todoist not configured"
    connector = TodoistConnector(api_key=_tk, db_pool=pool, timeout=10.0)
    cmd = TodoistConnector.build_item_update_command(task_id, labels=new_labels)
    result = await connector.commands([cmd])
    status = TodoistConnector.check_sync_status(result, [cmd["uuid"]])
    fail_msg = await _stage_chat_tool_outbox(pool, [cmd], status, "handoff_task")
    if fail_msg is not None:
        return fail_msg
    return f"Handed off {task_id} to {to_assignee}"


@aegis_tool
async def _exec_find_reference(
    pool: asyncpg.Pool, ctx: ToolContext, *, query: str, limit: int = 10
) -> str:
    """Search the \U0001f516 Reference project + knowledge-service for relevant items.

    Two sources: tasks labeled @reference + knowledge-service semantic
    search filtered to source_type='reference'.

    Phase 5: KS gains a real reference corpus when ClarifyFlow classifies
    items as 'reference' (ingest_reference_to_ks pushes body + URL + tags).
    This tool searches THAT corpus, with a Todoist title ILIKE fallback
    for items not yet ingested. Post-GTD-restructure the Todoist query
    is by @reference label, not project_id.
    """
    query = (query or "").strip()
    limit = int(limit or 10)
    if not query:
        return "Refused: empty query"
    out: list[str] = []
    if pool is not None:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, content FROM todoist_tasks "
                "WHERE '@reference' = ANY(labels) "
                "AND NOT is_completed "
                "AND content ILIKE $1 "
                "ORDER BY updated_at DESC LIMIT $2",
                f"%{query}%",
                limit,
            )
            for r in rows:
                out.append(f"- [reference:{r['id']}] {r['content']}")
    # KS pass — semantic search the reference corpus directly.
    if ctx.knowledge_connector:
        try:
            ks_results = await ctx.knowledge_connector.search(
                query,
                limit=limit,
                source_type="reference",
            )
            if ks_results:
                out.append("Semantic matches (Reference KB):")
                for item in ks_results[:limit]:
                    title = (item.get("title") or "").strip()[:120]
                    score = item.get("score") or item.get("similarity") or 0.0
                    cid = item.get("content_id") or item.get("id") or ""
                    out.append(f"- [{cid}] {title} (score={score:.2f})")
        except Exception as exc:  # noqa: BLE001
            logger.warning("find_reference_ks_failed", error=str(exc)[:200])
    if not out:
        return "No reference matches."
    return "\n".join(out)
