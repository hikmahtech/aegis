"""Admin reads and mutations over the problem hub (`services/hub.py`).

The Problems page is the operator's view of what AEGIS currently thinks is
wrong: one row per problem, its timeline, the sessions on it and the deploy or
maintenance windows in force.

Two rules hold this module together. Every mutation calls the SAME function the
chat tool and the worker call — closing, muting and merging live in `hub.py`,
so the page can never drift into a second implementation of a transition. And
nothing here writes Todoist: the projector owns that, and the sweep re-derives
the task from the problem within five minutes of any change made here.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from aegis.api.auth import verify_auth
from aegis.observability import log_audit
from aegis.services import hub_project
from aegis.services.hub import (
    close_problem,
    digest,
    list_problems,
    list_service_states,
    merge_problems,
    mute_problem,
    problem_detail,
    set_service_state,
    set_status,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/api/admin", dependencies=[Depends(verify_auth)], tags=["problems"])


async def _audit(request: Request, action: str, target_id: str, details: dict) -> None:
    """One audit row per mutation. `log_audit` never raises, so a missing audit
    table cannot cost the operator the action they asked for."""
    await log_audit(
        request.app.state.db_pool,
        actor="admin",
        action=action,
        target_type="problem",
        target_id=target_id,
        details=details,
    )


def _pool(request: Request):
    pool = request.app.state.db_pool
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    return pool


class MuteBody(BaseModel):
    hours: float = Field(default=24.0, gt=0, le=24 * 30)


class MergeBody(BaseModel):
    merge_id: str


class CloseBody(BaseModel):
    reason: str = "closed from the admin panel"


class ServiceStateBody(BaseModel):
    subject: str
    state: str
    subject_kind: str = "service"
    minutes: int | None = 30
    note: str = ""


@router.get("/problems")
async def get_problems(
    request: Request,
    status: str = "",
    subject: str = "",
    include_closed: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """The problem list, newest activity first. Live only unless
    `include_closed`."""
    return {
        "problems": await list_problems(
            _pool(request),
            status=status,
            subject=subject,
            include_closed=include_closed,
            limit=limit,
            offset=offset,
        )
    }


@router.get("/problems/digest")
async def get_digest(request: Request, hours: float = 24.0) -> dict[str, Any]:
    """What the hub saw in a window — the same query the daily briefing sends.
    Declared before `/problems/{problem_id}` so the literal path wins."""
    return await digest(_pool(request), hours=hours)


@router.get("/problems/{problem_id}")
async def get_problem_detail(request: Request, problem_id: str, events: int = 50) -> dict[str, Any]:
    """One problem with its timeline, links, sessions and active window."""
    detail = await problem_detail(_pool(request), problem_id, events=events)
    if detail is None:
        raise HTTPException(status_code=404, detail="problem_not_found")
    return detail


@router.post("/problems/{problem_id}/mute")
async def post_mute(request: Request, problem_id: str, body: MuteBody) -> dict[str, Any]:
    """Silence a problem for `hours`. Occurrences are still recorded and still
    counted — muting stops the projection and the investigation, not the
    record."""
    until = await mute_problem(_pool(request), problem_id, hours=body.hours, by="admin")
    if until is None:
        raise HTTPException(status_code=404, detail="problem_not_found_or_closed")
    await _audit(request, "problem_muted", problem_id, {"hours": body.hours})
    return {"problem_id": problem_id, "muted_until": until}


@router.post("/problems/{problem_id}/resolve")
async def post_resolve(request: Request, problem_id: str, body: CloseBody) -> dict[str, Any]:
    """Mark a problem resolved by hand. The projector closes its task on the
    next sweep, and the nightly close sweep retires it a week later."""
    moved = await set_status(
        _pool(request), problem_id, "resolved", reason=body.reason, source="admin"
    )
    if not moved:
        raise HTTPException(status_code=404, detail="problem_not_found_or_already_resolved")
    await _audit(request, "problem_resolved", problem_id, {"reason": body.reason})
    return {"problem_id": problem_id, "status": "resolved"}


@router.post("/problems/{problem_id}/close")
async def post_close(request: Request, problem_id: str) -> dict[str, Any]:
    """Close a resolved problem now instead of waiting for the nightly sweep,
    which frees its correlation key for a genuinely new problem.

    Projected first, and only this problem: a closed problem is never
    projected again, so closing one whose resolution has not reached its task
    yet would leave that task open for ever with no closing comment.
    """
    pool = _pool(request)
    try:
        await hub_project.project(pool, problem_id)
    except Exception as exc:  # noqa: BLE001 — Todoist being down must not block the close
        logger.warning("problem_close_project_failed", problem_id=problem_id, error=str(exc)[:200])
    if not await close_problem(pool, problem_id):
        raise HTTPException(status_code=409, detail="problem_not_found_or_not_resolved")
    await _audit(request, "problem_closed", problem_id, {})
    return {"problem_id": problem_id, "closed": True}


@router.post("/problems/{problem_id}/merge")
async def post_merge(request: Request, problem_id: str, body: MergeBody) -> dict[str, Any]:
    """Fold `merge_id` into this problem: its events, links and sessions move
    and it closes with a link back. The merged problem's own task is left for
    the operator — the projector never deletes a task it did not create."""
    try:
        result = await merge_problems(
            _pool(request), problem_id, body.merge_id, by="admin"
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _audit(request, "problems_merged", problem_id, result)
    return result


@router.get("/service-state")
async def get_service_state(request: Request) -> dict[str, Any]:
    """Every deploy / maintenance / degraded window in force."""
    return {"windows": await list_service_states(_pool(request))}


@router.put("/service-state")
async def put_service_state(request: Request, body: ServiceStateBody) -> dict[str, Any]:
    """Open or clear a window. `state: "ok"` clears it."""
    try:
        row = await set_service_state(
            _pool(request),
            body.subject,
            body.state,
            subject_kind=body.subject_kind,
            minutes=body.minutes if body.state != "ok" else None,
            set_by="admin",
            note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _audit(request, "service_state_set", f"{row['subject_kind']}:{row['subject']}", row)
    return row
