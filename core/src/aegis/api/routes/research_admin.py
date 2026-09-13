"""Admin → Research: the research lane's DB-owned config, with a validating
write path.

Five rows, one GET/PUT pair each, all under `/api/admin/research`:

* `topics` — the tracked-topic registry (`settings.intelligence_topics`),
  read leniently through `research_topics.parse_topics` and written whole
  under the registry's advisory lock (`research_topics.save_registry`), so an
  admin save cannot race a chat `track_topic`.
* `topics-config` — `research_topics_config`: per-priority attention
  thresholds and the digest size.
* `feeds-config` — `feeds_config`: feed health thresholds.
* `config` — `research_config`: research limits.
* `library-config` — `library_config`: library read limits.

Every PUT 400s on bad input rather than dropping it: the generic
`/api/settings` editor validates nothing, and a typo there becomes a silent
no-op (the `email_rules` lesson).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from aegis.api.auth import verify_auth
from aegis.services import (
    feeds_config,
    library_config,
    research_config,
    research_topics,
    topics_config,
)

router = APIRouter(
    prefix="/api/admin/research",
    tags=["research"],
    dependencies=[Depends(verify_auth)],
)


def _pool(request: Request) -> Any:
    return request.app.state.db_pool


@router.get("/topics")
async def get_topics(request: Request) -> dict[str, Any]:
    """Every tracked topic, with its live round and task when it has one."""
    pool = _pool(request)
    return {
        "topics": await research_topics.list_registry(pool),
        "priorities": list(research_topics.PRIORITIES),
        "config": await topics_config.get_topics_config(pool),
    }


@router.put("/topics")
async def put_topics(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Replace the registry. 400 on a bad entry; nothing is written then."""
    pool = _pool(request)
    try:
        await research_topics.save_registry(pool, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await get_topics(request)


@router.get("/topics-config")
async def get_topics_config_route(request: Request) -> dict[str, Any]:
    return await topics_config.get_topics_config(_pool(request))


@router.put("/topics-config")
async def put_topics_config_route(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    try:
        return await topics_config.save_topics_config(_pool(request), body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/feeds-config")
async def get_feeds_config_route(request: Request) -> dict[str, Any]:
    return await feeds_config.get_feeds_config(_pool(request))


@router.put("/feeds-config")
async def put_feeds_config_route(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    try:
        return await feeds_config.save_feeds_config(_pool(request), body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/config")
async def get_research_config_route(request: Request) -> dict[str, Any]:
    return await research_config.get_research_config(_pool(request))


@router.put("/config")
async def put_research_config_route(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    try:
        return await research_config.save_research_config(_pool(request), body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/library-config")
async def get_library_config_route(request: Request) -> dict[str, Any]:
    return await library_config.get_library_config(_pool(request))


@router.put("/library-config")
async def put_library_config_route(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    try:
        return await library_config.save_library_config(_pool(request), body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
