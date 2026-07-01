"""Observability endpoints — browse LLM and connector call telemetry."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from aegis.api.auth import verify_auth
from aegis.api.sql_filters import build_where

router = APIRouter(prefix="/api/observability", dependencies=[Depends(verify_auth)])


@router.get("/llm-calls")
async def list_llm_calls(
    request: Request,
    model: str | None = None,
    agent_id: str | None = None,
    purpose: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Browse LLM call telemetry."""
    pool = request.app.state.db_pool
    where, params = build_where({"model": model, "agent_id": agent_id, "purpose": purpose})
    idx = len(params) + 1
    params.extend([limit, offset])
    rows = await pool.fetch(
        f"SELECT * FROM llm_calls{where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx + 1}",
        *params,
    )
    return [dict(r) for r in rows]


@router.get("/llm-stats")
async def llm_stats(
    request: Request,
    model: str | None = None,
    agent_id: str | None = None,
    purpose: str | None = None,
) -> dict[str, Any]:
    """Aggregate LLM call statistics."""
    pool = request.app.state.db_pool
    where, params = build_where({"model": model, "agent_id": agent_id, "purpose": purpose})
    row = await pool.fetchrow(
        f"""SELECT COUNT(*) as total_calls,
                   COALESCE(SUM(input_tokens), 0) as total_prompt_tokens,
                   COALESCE(SUM(output_tokens), 0) as total_completion_tokens,
                   COALESCE(AVG(latency_ms), 0)::int as avg_latency_ms,
                   COALESCE(MAX(latency_ms), 0) as max_latency_ms
            FROM llm_calls{where}""",
        *params,
    )
    return dict(row)


@router.get("/connector-calls")
async def list_connector_calls(
    request: Request,
    connector: str | None = None,
    action: str | None = None,
    status: str | None = None,
    agent_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Browse connector call telemetry."""
    pool = request.app.state.db_pool
    where, params = build_where(
        {"connector": connector, "action": action, "status": status, "agent_id": agent_id}
    )
    idx = len(params) + 1
    params.extend([limit, offset])
    rows = await pool.fetch(
        f"SELECT * FROM connector_calls{where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx + 1}",
        *params,
    )
    return [dict(r) for r in rows]


@router.get("/connector-stats")
async def connector_stats(
    request: Request,
    connector: str | None = None,
    action: str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Aggregate connector call statistics."""
    pool = request.app.state.db_pool
    where, params = build_where(
        {"connector": connector, "action": action, "agent_id": agent_id}
    )
    row = await pool.fetchrow(
        f"""SELECT COUNT(*) as total_calls,
                   COALESCE(AVG(latency_ms), 0)::int as avg_latency_ms,
                   COUNT(*) FILTER (WHERE status = 'error') as error_count
            FROM connector_calls{where}""",
        *params,
    )
    return dict(row)


@router.get("/workflow-runs")
async def list_workflow_runs(
    request: Request,
    agent_id: str | None = None,
    workflow_type: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Browse workflow-run history (backed by the Temporal interceptor)."""
    pool = request.app.state.db_pool
    where, params = build_where(
        {"agent_id": agent_id, "workflow_type": workflow_type, "status": status}
    )
    idx = len(params) + 1
    safe_limit = min(max(limit, 1), 500)
    safe_offset = max(offset, 0)
    params.extend([safe_limit, safe_offset])
    rows = await pool.fetch(
        f"SELECT run_id, workflow_id, workflow_type, agent_id, parent_run_id, "
        f"status, started_at, completed_at, duration_ms, error, "
        f"input_summary, result_summary "
        f"FROM workflow_runs{where} "
        f"ORDER BY started_at DESC LIMIT ${idx} OFFSET ${idx + 1}",
        *params,
    )
    return [dict(r) for r in rows]
