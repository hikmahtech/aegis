"""Shared helpers for the manual flow-trigger admin routes.

The ``/api/admin/money`` and ``/api/admin/homelab`` routers each expose a
``POST /{flow}/run`` endpoint that maps a short flow slug to a Temporal
workflow class name and starts it. The mapping table differs per router but
the dispatch body (unknown-flow 400, manual workflow id, task queue) and the
"no temporal client → 503" guard are identical.

Workflow class names are passed as strings so Core never imports
``aegis_worker`` (only installed in the worker container); Temporal resolves
them against the worker's registered classes at dispatch time.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, Request
from temporalio.client import Client as TemporalClient


def require_temporal_client(request: Request) -> TemporalClient:
    """Return the connected Temporal client from app state, or 503."""
    client: TemporalClient | None = request.app.state.temporal_client
    if client is None:
        raise HTTPException(status_code=503, detail="Temporal service unavailable")
    return client


async def start_named_workflow(
    flow: str,
    cfg: dict,
    temporal_client: TemporalClient,
    flow_names: dict[str, str],
):
    """Start the Temporal workflow mapped to ``flow``.

    Raises 400 for an unknown flow slug. The workflow id is
    ``manual-{flow}-{8 hex}`` and the task queue is ``aegis-main``.
    """
    workflow_name = flow_names.get(flow)
    if workflow_name is None:
        raise HTTPException(status_code=400, detail=f"unknown flow: {flow}")
    return await temporal_client.start_workflow(
        workflow_name,
        cfg or {},
        id=f"manual-{flow}-{uuid.uuid4().hex[:8]}",
        task_queue="aegis-main",
    )
