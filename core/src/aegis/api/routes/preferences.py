"""User preferences that several lanes read — today, the clock.

``GET/PUT /api/admin/preferences/timezone`` is the validating write path for
the ``user_timezone`` settings row. ``services/user_time.user_zone`` stays the
one reader (a chat tool's "today", the daylog's day bounds, a dated heading in
the vault, the social planner); this route only makes sure what it reads is a
real zone name. The generic ``/api/settings`` editor stores anything, and
``user_zone`` reads a typo as UTC without a word — a silent five-and-a-half
hour shift is exactly the failure a 400 here prevents.
"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request

from aegis.api.auth import verify_auth
from aegis.services.settings_store import get_setting, put_setting
from aegis.services.user_time import SETTING, user_zone

router = APIRouter(
    prefix="/api/admin/preferences",
    tags=["preferences"],
    dependencies=[Depends(verify_auth)],
)


def validate_timezone(name: Any) -> str:
    """The zone name, or ValueError. Empty means UTC (the row is removed)."""
    if name is None:
        return ""
    if not isinstance(name, str):
        raise ValueError("timezone must be a zone name such as Europe/Berlin")
    name = name.strip()
    if not name:
        return ""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"{name!r} is not a known timezone (use a name such as Europe/Berlin)") from exc
    return name


@router.get("/timezone")
async def get_timezone_route(request: Request) -> dict[str, Any]:
    """The effective zone — what `user_zone` resolves, UTC when unset."""
    pool = request.app.state.db_pool
    value = await get_setting(pool, SETTING)
    stored = value if isinstance(value, str) else ""
    return {"timezone": stored, "effective": str((await user_zone(pool)).key)}


@router.put("/timezone")
async def put_timezone_route(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Set the zone. 400 on a name `ZoneInfo` does not know."""
    pool = request.app.state.db_pool
    try:
        name = validate_timezone((body or {}).get("timezone"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if name:
        await put_setting(pool, SETTING, name)
    else:
        await pool.execute("DELETE FROM settings WHERE key = $1", SETTING)
    return {"timezone": name, "effective": str((await user_zone(pool)).key)}
