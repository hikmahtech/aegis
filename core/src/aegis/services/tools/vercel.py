"""Vercel read-only chat tool executors (Pandora)."""

from __future__ import annotations

import json

import asyncpg

from aegis.services.tools.base import ToolContext
from aegis.services.tools.registry import aegis_tool


# `vercel-<name>` slugs in the resources table strip to the bare Vercel project
# name, which is what the v9/projects/{id_or_name} endpoint expects.
def _normalize_vercel_project(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("vercel-"):
        return value[len("vercel-") :]
    return value


@aegis_tool
async def _exec_vercel_get_project(pool: asyncpg.Pool, ctx: ToolContext, *, project: str) -> str:
    """Look up a Vercel project's metadata: framework, production domain, linked GitHub repo, etc. Use this when you need basic context about a project before investigating deployments.

    Args:
        project: Vercel project name (e.g. 'example-site') or resources slug (e.g. 'vercel-example-site').
    """
    if not ctx.vercel_connector:
        return json.dumps({"error": "vercel_connector_not_configured"})
    project = _normalize_vercel_project(project)
    if not project:
        return json.dumps({"error": "project is required"})
    result = await ctx.vercel_connector.get_project(project)
    return json.dumps(result)


@aegis_tool
async def _exec_vercel_list_deployments(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    project: str,
    limit: int = 10,
    since_hours: int | None = None,
    state: str | None = None,
) -> str:
    """List recent Vercel deployments for a project, with optional time-window and state filters. Use `state='ERROR'` to find failed deploys, `since_hours=24` to scope to the last day.

    Args:
        project: Vercel project name or 'vercel-<name>' slug.
        limit: Max deployments returned (1-100). Default 10.
        since_hours: Only return deployments created within the last N hours. Omit for no time filter.
        state: Filter by readyState: READY|ERROR|BUILDING|CANCELED|INITIALIZING|QUEUED. Case-insensitive. Omit for any state.
    """
    if not ctx.vercel_connector:
        return json.dumps({"error": "vercel_connector_not_configured"})
    project = _normalize_vercel_project(project)
    if not project:
        return json.dumps({"error": "project is required"})
    limit = int(limit)
    if since_hours is not None:
        try:
            since_hours = int(since_hours)
        except (TypeError, ValueError):
            return json.dumps({"error": "since_hours must be an integer"})
    result = await ctx.vercel_connector.list_deployments(
        project, limit=limit, since_hours=since_hours, state=state
    )
    return json.dumps(result)


@aegis_tool
async def _exec_vercel_get_deployment(
    pool: asyncpg.Pool, ctx: ToolContext, *, deployment_id: str
) -> str:
    """Fetch a single Vercel deployment by id (dpl_*). Surfaces errorCode/errorMessage/errorStep if the deploy ERROR'd, plus the git commit ref/sha/message that triggered it.

    Args:
        deployment_id: Vercel deployment uid (starts with 'dpl_').
    """
    if not ctx.vercel_connector:
        return json.dumps({"error": "vercel_connector_not_configured"})
    deployment_id = (deployment_id or "").strip()
    if not deployment_id:
        return json.dumps({"error": "deployment_id is required"})
    result = await ctx.vercel_connector.get_deployment(deployment_id)
    return json.dumps(result)


@aegis_tool
async def _exec_vercel_get_build_logs(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    deployment_id: str,
    limit: int = 100,
    errors_only: bool = False,
) -> str:
    """Fetch build event log for a Vercel deployment (newest first). Set errors_only=true to filter to stderr lines — useful for isolating the failure in a deploy that ERROR'd.

    Args:
        deployment_id: Vercel deployment uid (starts with 'dpl_').
        limit: Max events (1-1000). Default 100.
        errors_only: If true, only return stderr-typed events.
    """
    if not ctx.vercel_connector:
        return json.dumps({"error": "vercel_connector_not_configured"})
    deployment_id = (deployment_id or "").strip()
    if not deployment_id:
        return json.dumps({"error": "deployment_id is required"})
    limit = int(limit)
    errors_only = bool(errors_only)
    result = await ctx.vercel_connector.get_build_logs(
        deployment_id, limit=limit, errors_only=errors_only
    )
    return json.dumps(result)
