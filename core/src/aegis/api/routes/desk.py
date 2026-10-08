"""Admin endpoints for Maou's paper trading desk.

They were part of `routes/money.py` and keep its `/api/admin/money/desk*`
paths, so the admin page calls them unchanged. They live apart because the
desk stays in v1 when the books lane leaves it.

The desk holds real paper positions, so the page can show and cannot trade: the
only write is configuration — which market, which currency, which tax law
(`desk_rules`) — checked before it is stored.

A number is reported by whoever already computes it: the desk's score is
`trading_desk.month_summary`, the same call the monthly close makes; the desk's
positions come from `desk_math.replay`, the same replay the daily run uses to
decide what it holds.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from aegis.api.auth import verify_auth
from aegis.services import desk_rules, desk_view

router = APIRouter(
    prefix="/api/admin/money",
    tags=["desk"],
    dependencies=[Depends(verify_auth)],
)

# The reads live in `services/desk_view.py`, shared with the `desk_status` chat
# tool so the page and the desk's own agent can never disagree about the book.


@router.get("/desk")
async def desk_state(request: Request) -> dict:
    """The trading desk today: what it holds, what it is worth, how it is doing
    (`desk_view.snapshot`)."""
    return await desk_view.snapshot(request.app.state.db_pool)


@router.get("/desk/history")
async def desk_history(
    request: Request,
    # One trading month by default, so the page opens on something a reader
    # can hold in their head.
    limit: int = Query(desk_view.HISTORY_DAYS, ge=1, le=365),
) -> dict:
    """Each decision date the desk acted on, with the orders it wrote
    (`desk_view.history`)."""
    return await desk_view.history(request.app.state.db_pool, limit)


@router.get("/desk/series")
async def desk_series(request: Request) -> dict:
    """The desk's daily value beside its benchmarks, for the return chart
    (`desk_view.series`)."""
    return await desk_view.series(request.app.state.db_pool)


@router.get("/desk/rules")
async def desk_rules_state(request: Request) -> dict:
    """The desk's market and tax settings, as the desk itself reads them.

    The values come from `desk_math.Rules`, the same merge the daily run uses,
    so the form can never show a second opinion of what the desk believes.
    """
    return await desk_rules.read(request.app.state.db_pool)


@router.put("/desk/rules")
async def put_desk_rules(request: Request, body: dict[str, Any]) -> dict:
    """Save the desk's market and tax settings. 400 on anything that would not
    work, rather than a 200 that stores a typo and does nothing for months.

    This is a merge over the `trading-desk-daily` config, not a replacement:
    the knobs this page does not show keep their stored values. `schedule_sync`
    re-reads that row every few minutes and the desk reads it on every run, so
    a save takes effect without a deploy.
    """
    try:
        return await desk_rules.save(request.app.state.db_pool, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(
            status_code=404, detail="the trading desk has no activities row to configure"
        ) from exc
