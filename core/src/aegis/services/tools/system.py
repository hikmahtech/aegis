"""AEGIS's own operating surface as chat tools.

Scheduled activities and their run history, manual workflow triggers, new
schedules, the pending-interaction list, the status digest, and the two
operator-configuration writers (triage settings, runbook knowledge). What they
have in common is that the subject is AEGIS itself rather than the outside
world.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal
from uuid import uuid4

import asyncpg
import structlog
from pydantic import Field

from aegis.agent_tags import GENERALIST_TAG
from aegis.errors import error_text
from aegis.observability import log_audit
from aegis.services.agents import resolve_tag
from aegis.services.tools.base import ToolContext
from aegis.services.tools.registry import aegis_tool

logger = structlog.get_logger()


@aegis_tool
async def _exec_query_activities(
    pool: asyncpg.Pool, ctx: ToolContext, *, active_only: bool = True, limit: int = 20
) -> str:
    """List scheduled activities and their recent run history

    Args:
        active_only: Only show active activities
    """
    where = "WHERE a.active = TRUE" if active_only else ""
    rows = await pool.fetch(
        f"SELECT a.slug, a.workflow_type, a.schedule_cron, a.active, a.agent_id, "
        f"(SELECT max(started_at) FROM workflow_runs r "
        f" WHERE r.workflow_type = a.workflow_type) AS last_run "
        f"FROM activities a {where} ORDER BY a.slug LIMIT $1",
        limit,
    )
    return json.dumps([dict(r) for r in rows], default=str)


@aegis_tool
async def _exec_trigger_workflow(
    pool: asyncpg.Pool, ctx: ToolContext, *, workflow_type: str, params: dict | None = None
) -> str:
    """Trigger a Temporal workflow manually. Returns the workflow run ID. workflow_type must match an existing activities.workflow_type (e.g. DailyBriefingFlow, ClarifyFlow) — an unknown name is rejected with the list of valid values.

    Args:
        workflow_type: Which workflow to trigger, e.g. 'DailyBriefingFlow'
        params: Optional workflow parameters
    """
    if not ctx.temporal_client:
        return json.dumps({"error": "Temporal client not available"})
    from aegis.services.workflows import trigger_workflow

    result = await trigger_workflow(ctx.temporal_client, pool, workflow_type or "", params)
    return json.dumps(result, default=str)


@aegis_tool
async def _exec_create_schedule(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    workflow_type: str,
    cron: str,
    slug: str | None = None,
    config: dict | None = None,
) -> str:
    """Create a new recurring schedule for an existing flow type. Use when the user asks to run something on a cadence (e.g. 'also run the daily briefing at 7am'). Takes effect within ~5 minutes.

    Args:
        workflow_type: An existing flow class name, e.g. DailyBriefingFlow. Use query_activities to see valid types.
        cron: 5-field UTC cron, e.g. '30 2 * * *' (= 08:00 IST). Minimum interval 5 minutes.
        slug: Optional unique short name; auto-derived when omitted.
        config: Optional flow tuning knobs (same keys as the existing activity of this type).

    Returns:
        Inserts an activities row from NL-filled tool args; schedule_sync
        reconciles it into a live Temporal schedule on its ~300s tick — no
        worker restart needed.
    """
    workflow_type = (workflow_type or "").strip()
    cron = (cron or "").strip()
    valid_rows = await pool.fetch("SELECT DISTINCT workflow_type FROM activities ORDER BY 1")
    valid = [r["workflow_type"] for r in valid_rows]
    if workflow_type not in valid:
        return json.dumps(
            {"error": f"unknown workflow_type '{workflow_type}'; valid types: {', '.join(valid)}"}
        )
    fields = cron.split()
    if len(fields) != 5:
        return json.dumps({"error": "cron must have exactly 5 fields (min hour dom mon dow), UTC"})
    minute = fields[0]
    if minute == "*" or (minute.startswith("*/") and minute[2:].isdigit() and int(minute[2:]) < 5):
        return json.dumps({"error": "schedules more frequent than every 5 minutes are not allowed"})
    slug = (slug or "").strip() or f"nl-{workflow_type.lower()}-{uuid4().hex[:4]}"
    config = dict(config or {})
    config["created_by"] = "chat"
    # No calling agent: the generalist owns it, never an example id (#579).
    agent_id = ctx.agent_id or await resolve_tag(pool, GENERALIST_TAG)
    if not agent_id:
        return json.dumps({"error": "no agent to own the schedule — no active agent holds the gtd tag"})
    try:
        row = await pool.fetchrow(
            "INSERT INTO activities (slug, workflow_type, agent_id, schedule_cron, config, active) "
            "VALUES ($1,$2,$3,$4,$5,TRUE) "
            "RETURNING slug, workflow_type, agent_id, schedule_cron",
            slug,
            workflow_type,
            agent_id,
            cron,
            config,
        )
    except asyncpg.UniqueViolationError:
        return json.dumps({"error": f"slug '{slug}' already exists — pick another"})
    except asyncpg.ForeignKeyViolationError:
        return json.dumps({"error": f"agent '{agent_id}' not found"})
    await log_audit(
        pool,
        actor=f"chat:{agent_id}",
        action="activity_created",
        target_type="activity",
        target_id=slug,
        details={"workflow_type": workflow_type, "cron": cron},
    )
    return json.dumps(
        {
            "created": dict(row),
            "note": "live within ~5 minutes (schedule_sync tick); manage it on the admin Flows page",
        }
    )


@aegis_tool
async def _exec_list_interactions(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    agent_id: str | None = None,
    status: Literal["pending", "resolved", "expired"] | None = None,
    limit: Annotated[int, Field(ge=1, le=100)] | None = None,
) -> str:
    """List pending human-in-the-loop interactions (approvals, choices, inputs) for an agent. Use this when the user asks about pending decisions, approvals awaiting their response, or what needs their attention.

    Args:
        agent_id: Agent to filter by (defaults to the caller's agent).
        status: Filter by interaction status (default: pending).
        limit: Max rows to return (default 20, max 100).
    """
    agent_id = agent_id or ctx.agent_id
    if not agent_id:
        return json.dumps([])
    # Schema enum enforces this in production; guard is belt-and-suspenders
    # for direct test calls that bypass _validate_tool_args.
    if status not in ("pending", "resolved", "expired"):
        status = "pending"
    limit = max(1, min(int(20 if limit is None else limit), 100))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, kind, origin, prompt, status, created_at, resolved_at
            FROM interactions
            WHERE agent_id = $1 AND status = $2
            ORDER BY created_at DESC
            LIMIT $3
            """,
            agent_id,
            status,
            limit,
        )
    result = [
        {
            "id": str(r["id"]),
            "kind": r["kind"],
            "origin": r["origin"],
            "prompt": r["prompt"],
            "status": r["status"],
            "created_at": r["created_at"].isoformat(),  # NOT NULL per schema
            "resolved_at": r["resolved_at"].isoformat() if r["resolved_at"] else None,
        }
        for r in rows
    ]
    return json.dumps(result, default=str)


@aegis_tool
async def _exec_system_status(pool: asyncpg.Pool, ctx: ToolContext, *, hours: int = 24) -> str:
    """Aggregate system status: workflow runs by type/status, hard failures, runs that completed but actually failed (result_summary encodes an error), LLM token spend, pending interactions, and stuck infra services. Use when the user asks what ran, what broke, what's pending on them, or what we spent.

    Args:
        hours: Lookback window in hours (default 24, max 168).
    """
    from aegis.services.status_digest import get_status_digest

    hours = int(hours or 24)
    hours = min(max(hours, 1), 168)
    digest = await get_status_digest(pool, hours=hours)
    return json.dumps(digest, default=str)


_TRIAGE_SETTING_KEYS = {
    "sentry_ignored_projects": "triage_sentry_ignored_projects",
    "email_ignored_domains": "triage_ignored_email_domains",
    "notification_mode": "triage_notification_mode",
    "burst_threshold": "triage_burst_threshold",
}
_TRIAGE_LIST_SETTINGS = {"sentry_ignored_projects", "email_ignored_domains"}


@aegis_tool
async def _exec_configure_triage(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    setting: Literal[
        "sentry_ignored_projects",
        "email_ignored_domains",
        "notification_mode",
        "burst_threshold",
    ],
    action: Literal["add", "remove", "set", "get"],
    value: str | float | None = None,
) -> str:
    """Read or update triage configuration: ignored Sentry projects, ignored email domains, notification mode, burst threshold.

    Args:
        setting: Which triage setting to read or modify.
        action: add/remove items in a list, set a scalar value, or get the current value.
        value: Value to add/remove/set. Omit for get.
    """
    if setting not in _TRIAGE_SETTING_KEYS:
        return json.dumps({"error": f"Unknown setting: {setting}"})

    db_key = _TRIAGE_SETTING_KEYS[setting]
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", db_key)

    if action == "get":
        current = row["value"] if row else ([] if setting in _TRIAGE_LIST_SETTINGS else None)
        return json.dumps({"setting": setting, "current": current})

    if setting in _TRIAGE_LIST_SETTINGS:
        current = (row["value"] if row else None) or []
        if not isinstance(current, list):
            current = []
        if action == "add":
            if value is None:
                return json.dumps({"error": "value required for add"})
            item = str(value).strip()
            if item not in current:
                current = [*current, item]
        elif action == "remove":
            if value is None:
                return json.dumps({"error": "value required for remove"})
            current = [x for x in current if x != str(value).strip()]
        else:
            return json.dumps({"error": f"Use add/remove/get for list settings, not '{action}'"})
        new_val = current
    else:
        if action != "set":
            return json.dumps({"error": f"Use set/get for scalar settings, not '{action}'"})
        if value is None:
            return json.dumps({"error": "value required for set"})
        new_val = value

    await pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
        db_key,
        new_val,
    )
    return json.dumps({"ok": True, "setting": setting, "action": action, "current": new_val})


@aegis_tool
async def _exec_update_runbook(
    pool: asyncpg.Pool, ctx: ToolContext, *, target: str, content: str
) -> str:
    """Update or add operational runbook knowledge for alert types or projects.

    Args:
        target: What to update, e.g. 'alert_type:ServiceDown', 'project:bcp'
        content: The runbook content to add
    """
    if not ctx.knowledge_connector:
        return json.dumps({"error": "Knowledge service not available"})

    if not target or not content:
        return json.dumps({"error": "Both target and content are required"})

    # ponytail: runbook knowledge is stored as a searchable content chunk
    # (no knowledge graph). gather_alert_knowledge finds it via chunk search.
    try:
        await ctx.knowledge_connector.ingest_content(
            url=f"aegis://runbook/{target}",
            title=f"Runbook: {target}",
            source_type="runbook",
            raw_text=content,
            tags=["runbook", target],
        )
        return json.dumps({"ok": True, "target": target})
    except Exception as exc:
        logger.warning("update_runbook_failed", error=error_text(exc, 500))
        return json.dumps({"ok": False, "error": error_text(exc, 500)})
