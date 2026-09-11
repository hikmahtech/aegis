"""``POST /api/hub/events`` — report a signal to the problem hub from outside.

A deploy job, a boot script, or a test can say what it saw without a bespoke
route. Same body shape as :class:`aegis.services.hub.Event`, same token as the
alert webhook (``X-Alert-Token`` or ``Authorization: Bearer``), because the
same producers use both. The blank-secret-means-open legacy default is kept
for the same reason and with the same warning (#88, #304): set the secret
wherever this port is reachable by anything you do not trust.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from aegis.api.auth import alert_token_ok
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.services.hub import Event, ingest_event, set_service_state

logger = structlog.get_logger()

router = APIRouter(prefix="/api/hub", tags=["hub"])


def _check_token(request: Request, settings: Settings, what: str) -> None:
    if settings.alert_webhook_secret and not alert_token_ok(
        request, settings.alert_webhook_secret
    ):
        logger.warning(f"hub_{what}_bad_token")
        raise HTTPException(status_code=401, detail="bad_token")


def _pool(request: Request):
    pool = request.app.state.db_pool
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    return pool


class ServiceStateBody(BaseModel):
    subject: str
    subject_kind: str = "service"
    state: str
    minutes: int | None = None
    note: str = ""
    set_by: str = "api"


@router.post("/service-state")
async def post_service_state(
    body: ServiceStateBody,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Declare a subject deploying / in maintenance / degraded / ok. The
    Ansible deploy role calls this at the top and bottom of a rollout."""
    _check_token(request, settings, "service_state")
    try:
        return await set_service_state(
            _pool(request),
            body.subject,
            body.state,
            subject_kind=body.subject_kind,
            minutes=body.minutes,
            set_by=body.set_by,
            note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class EventBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source: str
    external_id: str
    kind: str = "occurrence"
    title: str
    subject: str = ""
    subject_kind: str = ""
    klass: str = Field(default="", alias="class")
    severity: str = "warning"
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime | None = None
    # A uuid, validated here: `ingest_event` casts it to `uuid` in SQL, so a
    # malformed one used to reach asyncpg and surface as a 500 rather than
    # the 400 a caller can act on.
    problem_id: uuid.UUID | None = None


@router.post("/events")
async def post_event(
    body: EventBody,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Record one event. It only records: it starts no investigation and
    projects nothing inline, whatever the hub decides. The decision comes back
    as `investigate` in the response, and the sweep gives the problem its task
    within five minutes. The alert webhook is the route that acts on
    `investigate`; this one stays a plain door for reports, because an event
    from outside carries no alert dict for an investigation to work from, and
    a route that is open when the secret is blank must not be able to start a
    billed investigation."""
    _check_token(request, settings, "event")
    try:
        result = await ingest_event(_pool(request), Event(**body.model_dump(by_alias=False)))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result.to_dict()
