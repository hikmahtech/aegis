"""Admin endpoints for Todoist sync + outbox visibility.

Todoist is the canonical GTD task store; this surface answers "is the sync
loop healthy and has any write been lost?" — most importantly exposing
todoist_outbox rows stuck in status='failed', which previously had no reader
anywhere (a permanently failed write silently lost the captured task).
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.api.sql_filters import build_where
from aegis.config import Settings
from aegis.observability import log_audit

router = APIRouter(
    prefix="/api/admin/todoist",
    tags=["todoist"],
    dependencies=[Depends(verify_auth)],
)


@router.get("/config")
async def get_todoist_config(
    request: Request, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Todoist config status: api_key set + source, and the managed project ids."""
    from aegis.services.todoist_config import todoist_config_status

    return await todoist_config_status(request.app.state.db_pool, settings)


@router.put("/config")
async def put_todoist_config(
    request: Request, body: dict[str, Any], settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Set the Todoist API key (omitted → keep) and/or the managed project ids."""
    from aegis.services.todoist_config import save_todoist_config, todoist_config_status

    pool = request.app.state.db_pool
    api_key = body.get("api_key") if "api_key" in body else None
    projects = body.get("projects") if "projects" in body else None
    await save_todoist_config(pool, settings, api_key=api_key, projects=projects)
    return await todoist_config_status(pool, settings)


@router.get("/gtd-rules")
async def get_gtd_rules_route(request: Request) -> dict[str, Any]:
    """The GTD clarify taxonomy (source-tag → assignee / contexts / skip-inbox)."""
    from aegis.services.gtd_rules import SOURCE_TAGS, get_gtd_rules

    return {"source_tags": SOURCE_TAGS, **await get_gtd_rules(request.app.state.db_pool)}


@router.put("/gtd-rules")
async def put_gtd_rules_route(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Save the GTD taxonomy (assignee/contexts/skip_inbox maps); returns merged."""
    from aegis.services.gtd_rules import SOURCE_TAGS, save_gtd_rules

    return {"source_tags": SOURCE_TAGS, **await save_gtd_rules(request.app.state.db_pool, body)}


@router.get("/content-routes")
async def get_content_routes_route(request: Request) -> dict[str, Any]:
    """Content-routing rules: regex/prefix/contains on the task title → assignee /
    contexts / gate. Complements gtd-rules (which routes by source_tag)."""
    from aegis.services.content_routes import MATCH_MODES, get_content_routes

    routes = await get_content_routes(request.app.state.db_pool)
    return {"match_modes": list(MATCH_MODES), "routes": routes}


@router.put("/content-routes")
async def put_content_routes_route(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Replace the ordered content-routing rules. 400 on a malformed rule/regex."""
    from aegis.services.content_routes import MATCH_MODES, save_content_routes

    try:
        routes = await save_content_routes(request.app.state.db_pool, body.get("routes") or [])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"match_modes": list(MATCH_MODES), "routes": routes}


@router.post("/content-routes/preview")
async def preview_content_route(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Given a single {match, value}, return the current Inbox task titles it
    matches — the live safety-net shown before a rule is saved. Matching is done
    in Python (inbox is small) to sidestep cross-engine regex surprises."""
    from aegis.services.content_routes import compile_pattern

    try:
        pattern = compile_pattern(str(body.get("match") or ""), str(body.get("value") or ""))
        rx = re.compile(pattern)
    except (ValueError, re.error) as exc:
        raise HTTPException(status_code=400, detail=f"invalid pattern: {exc}") from exc

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        inbox_id = await conn.fetchval(
            "SELECT value->>'inbox' FROM settings WHERE key='todoist_managed_project_ids'"
        )
        rows = []
        if inbox_id:
            rows = await conn.fetch(
                "SELECT content FROM todoist_tasks "
                "WHERE project_id=$1 AND NOT is_completed "
                "ORDER BY updated_at DESC LIMIT 500",
                inbox_id,
            )
    matches = [r["content"] for r in rows if r["content"] and rx.search(r["content"])]
    return {"pattern": pattern, "matches": matches[:50], "match_count": len(matches)}


@router.post("/content-routes/suggest")
async def suggest_content_route(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Draft a regex from example task titles (LLM). A convenience: the returned
    pattern is a SUGGESTION the user edits + previews before saving — never
    auto-applied. Returns {pattern: null, error} instead of 500 on any LLM issue."""
    examples = [str(e).strip() for e in (body.get("examples") or []) if str(e).strip()]
    if not examples:
        raise HTTPException(status_code=400, detail="at least one example is required")
    llm = getattr(request.app.state, "llm", None)
    if llm is None:
        return {"pattern": None, "error": "LLM backend not configured"}

    from aegis.llm import parse_llm_json, tier_to_model

    try:
        model = tier_to_model("fast")
    except KeyError:
        try:
            model = tier_to_model("balanced")
        except KeyError:
            model = "gemma4:e2b"

    joined = "\n".join(f"- {e}" for e in examples[:20])
    prompt = (
        "You write POSIX/PCRE-compatible regular expressions for matching task "
        "titles. Given these example titles that should ALL match:\n"
        f"{joined}\n\n"
        'Return JSON ONLY: {"pattern": "<a single regex>"}. Keep it as simple and '
        "specific as possible; prefer an anchored prefix (^) when the examples "
        "share a leading token. No explanation."
    )
    try:
        result = await llm.think(
            prompt, model=model, max_tokens=200, purpose="content_route_suggest"
        )
    except Exception as exc:  # noqa: BLE001 — convenience endpoint, never 500
        return {"pattern": None, "error": f"LLM error: {str(exc)[:200]}"}
    raw = result.get("response", "") if isinstance(result, dict) else str(result)
    pattern = str((parse_llm_json(raw) or {}).get("pattern") or "").strip()
    if not pattern:
        return {"pattern": None, "error": "could not derive a pattern"}
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return {"pattern": None, "error": f"LLM produced an invalid regex: {exc}"}
    matched = [e for e in examples if rx.search(e)]
    return {
        "pattern": pattern,
        "match": "regex",
        "matches_examples": matched,
        "all_examples_match": len(matched) == len(examples),
    }


@router.get("/state")
async def todoist_state(request: Request) -> dict:
    """Sync watermarks, projection counts, and outbox health in one call."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        sync = await conn.fetchrow(
            "SELECT key, last_full_sync_at, last_incremental_at "
            "FROM todoist_sync_state WHERE key='main'"
        )
        outbox_counts = await conn.fetch(
            "SELECT status, count(*) AS n FROM todoist_outbox GROUP BY status"
        )
        oldest_pending = await conn.fetchval(
            "SELECT EXTRACT(epoch FROM now() - min(created_at))::int "
            "FROM todoist_outbox WHERE status='pending'"
        )
        failed_recent = await conn.fetch(
            "SELECT id, temp_id, command->>'type' AS command_type, "
            "attempt_count, last_attempt_at, created_at "
            "FROM todoist_outbox WHERE status='failed' "
            "ORDER BY created_at DESC LIMIT 50"
        )
        managed = await conn.fetchval(
            "SELECT value FROM settings WHERE key='todoist_managed_project_ids'"
        )
        open_tasks = await conn.fetchval(
            "SELECT count(*) FROM todoist_tasks WHERE NOT is_completed"
        )
        completed_7d = await conn.fetchval(
            "SELECT count(*) FROM todoist_tasks "
            "WHERE is_completed AND completed_at > now() - interval '7 days'"
        )
        unclarified = await conn.fetchval(
            "SELECT count(*) FROM todoist_tasks "
            "WHERE NOT is_completed AND source_tag IS NOT NULL "
            "AND last_clarified_at IS NULL"
        )
    return {
        "sync": dict(sync) if sync else None,
        "outbox": {
            "counts": {r["status"]: r["n"] for r in outbox_counts},
            "oldest_pending_age_seconds": oldest_pending,
            "failed_recent": [dict(r) for r in failed_recent],
        },
        "tasks": {
            "open": int(open_tasks or 0),
            "completed_7d": int(completed_7d or 0),
            "pending_clarify": int(unclarified or 0),
        },
        "managed_projects": managed if isinstance(managed, dict) else None,
    }


@router.get("/tasks")
async def list_tasks(
    request: Request,
    project_id: str | None = None,
    status: str | None = None,
    assignee: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List Todoist tasks with optional filters — powers the inbox/task list view.

    ``status`` is one of open|completed (maps to is_completed); any other
    value is ignored (no filter). ``assignee`` filters on assignee_label.
    """
    pool = request.app.state.db_pool
    where, params = build_where({"project_id": project_id, "assignee_label": assignee})
    if status in ("open", "completed"):
        conj = "AND" if where else "WHERE"
        where = f"{where} {conj} is_completed = ${len(params) + 1}"
        params.append(status == "completed")
    idx = len(params) + 1
    params.append(limit)
    rows = await pool.fetch(
        "SELECT id, content, description, project_id, due_date, priority, labels, "
        "is_completed, assignee_label, source_tag, last_clarified_at "
        f"FROM todoist_tasks{where} ORDER BY updated_at DESC LIMIT ${idx}",
        *params,
    )
    return [dict(r) for r in rows]


@router.get("/projects")
async def list_projects(request: Request) -> list[dict[str, Any]]:
    """List Todoist projects — powers a project picker (replaces raw project-id inputs)."""
    pool = request.app.state.db_pool
    rows = await pool.fetch(
        "SELECT id, name, parent_id, is_managed, is_archived, order_idx "
        "FROM todoist_projects ORDER BY order_idx NULLS LAST, name"
    )
    return [dict(r) for r in rows]


@router.get("/labels")
async def list_labels(request: Request) -> list[dict[str, Any]]:
    """List Todoist labels — powers a label picker."""
    pool = request.app.state.db_pool
    rows = await pool.fetch("SELECT id, name, color, raw FROM todoist_labels ORDER BY name")
    return [dict(r) for r in rows]


@router.get("/clarify-log")
async def list_clarify_log(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    applied: bool | None = None,
) -> list[dict[str, Any]]:
    """Recent GTD clarify decisions (most recent first), joined to task content."""
    pool = request.app.state.db_pool
    where = " WHERE l.applied = $1" if applied is not None else ""
    params: list[Any] = [applied] if applied is not None else []
    idx = len(params) + 1
    params.append(limit)
    rows = await pool.fetch(
        "SELECT l.id, l.todoist_task_id, l.pass, l.source_tag, l.classification, "
        "l.confidence, l.assignee, l.contexts, l.reason, l.user_hint, l.llm_model, "
        "l.applied, l.created_at, t.content AS task_content "
        "FROM gtd_clarify_log l LEFT JOIN todoist_tasks t ON t.id = l.todoist_task_id"
        f"{where} ORDER BY l.created_at DESC LIMIT ${idx}",
        *params,
    )
    return [dict(r) for r in rows]


@router.get("/clarify-log/{log_id}")
async def get_clarify_log_detail(log_id: int, request: Request) -> dict[str, Any]:
    """Single clarify decision, all columns, joined to task content."""
    pool = request.app.state.db_pool
    row = await pool.fetchrow(
        "SELECT l.*, t.content AS task_content FROM gtd_clarify_log l "
        "LEFT JOIN todoist_tasks t ON t.id = l.todoist_task_id WHERE l.id = $1",
        log_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Clarify log entry not found")
    return dict(row)


@router.post("/tasks/{task_id}/reclarify")
async def reclarify_task(task_id: str, request: Request) -> dict[str, Any]:
    """Null out last_clarified_at so the next ClarifyFlow run reclassifies this task."""
    pool = request.app.state.db_pool
    row = await pool.fetchrow(
        "UPDATE todoist_tasks SET last_clarified_at = NULL WHERE id = $1 "
        "RETURNING id, content, last_clarified_at",
        task_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    await log_audit(
        pool,
        actor="api:todoist",
        action="task_reclarify_requested",
        target_type="todoist_task",
        target_id=task_id,
    )
    return dict(row)
