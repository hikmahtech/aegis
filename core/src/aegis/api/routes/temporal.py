"""Temporal proxy endpoints — proxy workflow queries through the backend."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request

from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.config import Settings

router = APIRouter(prefix="/api/temporal", dependencies=[Depends(verify_auth)])


@router.get("/workflows")
async def list_workflows(
    request: Request,
    settings: Settings = Depends(get_settings),
    page_size: int = 30,
) -> dict[str, Any]:
    """List recent workflow executions via Temporal UI HTTP API."""
    base = settings.temporal_api_url.rstrip("/")
    url = f"{base}/api/v1/namespaces/default/workflows"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params={"pageSize": page_size})
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return {"executions": [], "error": f"Cannot reach Temporal API at {base}"}


@router.get("/workflows/{workflow_id}")
async def workflow_detail(
    workflow_id: str,
    settings: Settings = Depends(get_settings),
    run_id: str | None = None,
) -> dict[str, Any]:
    """Describe + full event history for one execution (detail view).

    Merges the Temporal UI HTTP API's describe and history calls so the admin
    detail page can render any workflow type generically from the raw event
    stream. ponytail: workflow ids containing '/' aren't supported (AEGIS ids
    never do); switch to a {workflow_id:path} route if that ever changes.
    """
    base = settings.temporal_api_url.rstrip("/")
    root = f"{base}/api/v1/namespaces/default/workflows/{workflow_id}"
    params = {"execution.runId": run_id} if run_id else None

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            describe, history = await asyncio.gather(
                client.get(root, params=params),
                client.get(f"{root}/history", params=params),
            )
            describe.raise_for_status()
            history.raise_for_status()
            return {"describe": describe.json(), "history": history.json()}
    except Exception:
        return {
            "describe": None,
            "history": None,
            "error": f"Cannot reach Temporal API at {base}",
        }


@router.get("/config")
async def temporal_config(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    """Return service UI URLs for frontend links."""
    return {
        "temporal_ui_url": settings.temporal_ui_url,
        "knowledge_ui_url": settings.knowledge_ui_url,
        # Browser-facing Postiz URL (postiz_url is the server-side/internal one).
        "postiz_ui_url": settings.postiz_public_url,
    }
