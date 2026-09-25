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
from aegis.api.deps import get_pool
from aegis.api.settings_routes import settings_row_routes
from aegis.services import (
    feeds_config,
    library_config,
    news_overview,
    research_areas,
    research_config,
    research_topics,
    topics_config,
)

router = APIRouter(
    prefix="/api/admin/research",
    tags=["research"],
    dependencies=[Depends(verify_auth)],
)


@router.get("/topics")
async def get_topics(request: Request) -> dict[str, Any]:
    """Every tracked topic, with its live round and task when it has one."""
    pool = get_pool(request)
    return {
        "topics": await research_topics.list_registry(pool),
        "priorities": list(research_topics.PRIORITIES),
        "config": await topics_config.get_topics_config(pool),
        "areas": [
            {"name": a.name, "why": a.why, "cadence": a.cadence, "cap": a.cap, "topics": list(a.topics)}
            for a in await research_topics.load_areas(pool)
        ],
        "cadences": list(research_areas.CADENCES),
    }


@router.put("/topics")
async def put_topics(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Replace the registry. 400 on a bad entry; nothing is written then."""
    pool = get_pool(request)
    try:
        await research_topics.save_registry(pool, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await get_topics(request)


@router.post("/story-feedback")
async def story_feedback(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """The owner reacted to a message (#675): comms forwards `{channel, ts,
    reaction}` for every owner reaction on a message it did not write, and
    this records it when the message is an area story and the reaction a
    verdict. `matched: false` is the normal answer for any other message."""
    matched = await research_areas.record_verdict(
        get_pool(request),
        channel=str(body.get("channel") or ""),
        ts=str(body.get("ts") or ""),
        reaction=str(body.get("reaction") or ""),
    )
    return {"matched": matched}


@router.get("/scorecard")
async def get_scorecard(request: Request) -> dict[str, Any]:
    """Per area, the last 30 days: shown, 👍, 👎, saved, and idle areas."""
    return {"areas": await research_areas.scorecard(get_pool(request))}


@router.get("/stories")
async def get_stories(request: Request, area: str = "", limit: int = 100) -> dict[str, Any]:
    """The area stories the brief showed, newest first, with verdicts."""
    return {"stories": await news_overview.stories(get_pool(request), area=area, limit=limit)}


@router.get("/watchers")
async def get_watchers(request: Request) -> dict[str, Any]:
    """Every scheduled source filing onto a tracked topic: state, last run,
    area, recent items, and what keeps its items from the brief."""
    return {"watchers": await news_overview.watchers(get_pool(request))}


settings_row_routes(
    router,
    "/topics-config",
    get=topics_config.get_topics_config,
    save=topics_config.save_topics_config,
    doc="`research_topics_config`: per-priority attention thresholds and the digest size.",
)

settings_row_routes(
    router,
    "/feeds-config",
    get=feeds_config.get_feeds_config,
    save=feeds_config.save_feeds_config,
    doc="`feeds_config`: how many failed fetches, and how many silent days, make a feed a finding.",
)

settings_row_routes(
    router,
    "/config",
    get=research_config.get_research_config,
    save=research_config.save_research_config,
    doc="`research_config`: the research lane's depth, page, report and citation limits.",
)

settings_row_routes(
    router,
    "/library-config",
    get=library_config.get_library_config,
    save=library_config.save_library_config,
    doc="`library_config`: how much of a book one read may return.",
)
