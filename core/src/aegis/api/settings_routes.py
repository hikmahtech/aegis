"""One GET/PUT pair for a settings row, registered from one call.

Seventeen admin endpoints edit one `settings` row each (`services/config_rows.py`),
and their route handlers were byte-identical except for the module name: read
the row, or validate-and-save it and turn a `ValueError` into a 400. Writing
that out per row is where the family drifts — a pair that forgets the
`try`/`except` answers 500 on a typo, and one operator-facing knob loses the
guarantee the rest have.

So the pair is declared, not written::

    settings_row_routes(
        router, "/feeds-config",
        get=feeds_config.get_feeds_config,
        save=feeds_config.save_feeds_config,
        doc="Feed health thresholds.",
    )

Three optional hooks cover everything the seventeen actually differ by, and
each is used by several of them:

``body``
    The value inside the request body, when the page posts the row under a key
    (``{"routes": [...]}``) rather than as the body itself.
``view``
    What the page sees, when the row alone is not enough — the defaults under
    it, the vocabulary it may use, the senders it can be written against. Given
    ``(pool, row)``; may be async.
``audit``
    An `audit_log` row after a successful save. Given ``(request, row)``.

The row's own `merge` / `validate` stay in its service module: those are the
domain rules, and this is only the plumbing around them.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from aegis.api.deps import get_pool


def settings_row_routes(
    router: APIRouter,
    path: str,
    *,
    get: Callable[[Any], Awaitable[Any]],
    save: Callable[[Any, Any], Awaitable[Any]],
    doc: str = "",
    body: Callable[[dict[str, Any]], Any] | None = None,
    view: Callable[[Any, Any], Any] | None = None,
    audit: Callable[[Request, Any], Awaitable[None]] | None = None,
) -> None:
    """Register `GET path` and `PUT path` on `router` for one settings row."""

    async def render(pool: Any, row: Any) -> Any:
        if view is None:
            return row
        out = view(pool, row)
        return await out if inspect.isawaitable(out) else out

    @router.get(path, description=doc)
    async def read_row(pool: Any = Depends(get_pool)) -> Any:
        return await render(pool, await get(pool))

    @router.put(path, description=doc)
    async def write_row(
        request: Request, payload: dict[str, Any], pool: Any = Depends(get_pool)
    ) -> Any:
        try:
            row = await save(pool, payload if body is None else body(payload))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if audit is not None:
            await audit(request, row)
        return await render(pool, row)
