"""Vercel read-only chat tool executors (Pandora)."""

from __future__ import annotations

import json

import asyncpg

from aegis.services.tools.base import ToolContext


# `vercel-<name>` slugs in the resources table strip to the bare Vercel project
# name, which is what the v9/projects/{id_or_name} endpoint expects.
def _normalize_vercel_project(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("vercel-"):
        return value[len("vercel-") :]
    return value


async def _exec_vercel_get_project(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    if not ctx.vercel_connector:
        return json.dumps({"error": "vercel_connector_not_configured"})
    project = _normalize_vercel_project(args.get("project", ""))
    if not project:
        return json.dumps({"error": "project is required"})
    result = await ctx.vercel_connector.get_project(project)
    return json.dumps(result)


async def _exec_vercel_list_deployments(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    if not ctx.vercel_connector:
        return json.dumps({"error": "vercel_connector_not_configured"})
    project = _normalize_vercel_project(args.get("project", ""))
    if not project:
        return json.dumps({"error": "project is required"})
    limit = int(args.get("limit", 10))
    since_hours = args.get("since_hours")
    if since_hours is not None:
        try:
            since_hours = int(since_hours)
        except (TypeError, ValueError):
            return json.dumps({"error": "since_hours must be an integer"})
    state = args.get("state")
    result = await ctx.vercel_connector.list_deployments(
        project, limit=limit, since_hours=since_hours, state=state
    )
    return json.dumps(result)


async def _exec_vercel_get_deployment(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    if not ctx.vercel_connector:
        return json.dumps({"error": "vercel_connector_not_configured"})
    deployment_id = (args.get("deployment_id") or "").strip()
    if not deployment_id:
        return json.dumps({"error": "deployment_id is required"})
    result = await ctx.vercel_connector.get_deployment(deployment_id)
    return json.dumps(result)


async def _exec_vercel_get_build_logs(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    if not ctx.vercel_connector:
        return json.dumps({"error": "vercel_connector_not_configured"})
    deployment_id = (args.get("deployment_id") or "").strip()
    if not deployment_id:
        return json.dumps({"error": "deployment_id is required"})
    limit = int(args.get("limit", 100))
    errors_only = bool(args.get("errors_only", False))
    result = await ctx.vercel_connector.get_build_logs(
        deployment_id, limit=limit, errors_only=errors_only
    )
    return json.dumps(result)
