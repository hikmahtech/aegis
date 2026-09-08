"""Observability endpoints — browse LLM and connector call telemetry."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from aegis.api.auth import verify_auth
from aegis.api.sql_filters import build_where
from aegis.services.status_digest import get_status_digest

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
    """Browse LLM call telemetry — raw `llm_calls` rows, newest first.

    The UI charts the aggregate (`/llm-stats`); this is the row-level lane a
    spend/latency audit actually reads ("which calls burned the tokens").

    This endpoint is intentionally curl/ops-only, no UI consumer.
    """
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
                   COALESCE(MAX(latency_ms), 0) as max_latency_ms,
                   COALESCE(SUM(cost_usd), 0)::float8 as total_cost_usd,
                   COUNT(*) FILTER (WHERE cost_usd IS NULL) as unpriced_calls
            FROM llm_calls{where}""",
        *params,
    )
    return dict(row)


@router.get("/llm-spend")
async def llm_spend(
    request: Request,
    hours: float = 24.0,
    group_by: str = "model",
) -> dict[str, Any]:
    """What the last `hours` cost, grouped by `model`, `purpose` or `agent_id`.

    The cost is the one the LiteLLM proxy computed per call, so this is
    AEGIS's own accounting of what it asked for — not a reconciliation of the
    provider's invoice, which prices caching and rounding its own way.

    `unpriced` counts calls with no cost recorded (a backend that is not the
    proxy, or a call that failed before reaching a model). It is reported
    beside the total rather than folded into it: a spend figure that silently
    treats unknown as zero is the one that gets believed and is wrong.
    """
    if group_by not in {"model", "purpose", "agent_id"}:
        raise HTTPException(status_code=400, detail="group_by must be model, purpose or agent_id")
    pool = request.app.state.db_pool
    rows = await pool.fetch(
        f"SELECT COALESCE({group_by}, '(none)') AS key, count(*) AS calls, "
        "       COALESCE(SUM(cost_usd), 0)::float8 AS usd, "
        "       COALESCE(SUM(COALESCE(input_tokens,0) + COALESCE(output_tokens,0)), 0) AS tokens, "
        "       count(*) FILTER (WHERE cost_usd IS NULL) AS unpriced "
        "FROM llm_calls WHERE created_at > now() - make_interval(secs => $1) "
        "GROUP BY 1 ORDER BY usd DESC, calls DESC",
        max(0.0, float(hours)) * 3600.0,
    )
    groups = [dict(r) for r in rows]
    return {
        "hours": hours,
        "group_by": group_by,
        "total_usd": round(sum(g["usd"] for g in groups), 6),
        "unpriced_calls": sum(g["unpriced"] for g in groups),
        "groups": groups,
    }


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
    """Browse connector call telemetry — raw `connector_calls` rows, newest first.

    The UI charts the aggregate (`/connector-stats`); this is the row-level
    lane for "which connector call failed, and with what error".

    This endpoint is intentionally curl/ops-only, no UI consumer.
    """
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


@router.get("/status-digest")
async def status_digest(request: Request, hours: int = 24) -> dict[str, Any]:
    """Aggregate 'what ran / what broke / what's pending / what did we spend'
    for the last `hours`. Shared by the `system_status` chat tool
    (aegis.services.chat) and Slack `/status` (aegis_comms has no
    aegis-core dependency, so it reaches this over HTTP)."""
    pool = request.app.state.db_pool
    safe_hours = min(max(hours, 1), 168)
    return await get_status_digest(pool, hours=safe_hours)


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
