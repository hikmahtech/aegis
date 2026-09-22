"""The trading desk, as its own agent can see it in chat.

The desk (`services/trading_desk.py`) runs every weekday as Maou, and until
this tool nothing let Maou read it back: asked for the current holdings, it
could only read the ledger and reported "no securities holdings recorded" while
the desk held a paper portfolio. This reads the same `desk_view` the admin
Trading desk page reads, so the two cannot disagree. It only reads.
"""

from __future__ import annotations

import json
from typing import Annotated

import asyncpg
import structlog
from pydantic import Field

from aegis.errors import error_text
from aegis.services import desk_view
from aegis.services.tools.base import ToolContext
from aegis.services.tools.registry import aegis_tool

logger = structlog.get_logger()


@aegis_tool
async def _exec_desk_status(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    history_days: Annotated[int, Field(ge=0, le=30)] | None = None,
) -> str:
    """Read the paper trading desk you run: what it holds now, what the book is worth, cash, gain against the starting capital, orders waiting to fill, the latest plan and what it held back, this month's score against the benchmark, and any open problems. Read-only; the desk trades on its own schedule and this cannot place, change or cancel an order. Use it whenever the user asks about holdings, positions, trades, the portfolio or how the desk is doing — the ledger does not hold these.

    Args:
        history_days: Also return the last N decision dates (1-30) with the orders written on each. Omit or 0 for the current state only.

    Returns:
        JSON: `desk_view.snapshot` (the admin page's own read), plus
        `history` when asked. `configured: false` means no trading calendar
        is set, so the desk runs nothing. `score: null` means no order has
        filled yet this month, not a zero result. A failed read is reported
        as `{"error": ...}`, never as an empty book.
    """
    try:
        out = await desk_view.snapshot(pool)
        if history_days:
            out["history"] = (await desk_view.history(pool, int(history_days)))["days"]
    except Exception as exc:  # noqa: BLE001 — a failed read is an answer, not a crash
        logger.warning("desk_status_failed", error=error_text(exc))
        return json.dumps({"error": f"could not read the desk: {error_text(exc)}"})
    return json.dumps(out, default=str)
