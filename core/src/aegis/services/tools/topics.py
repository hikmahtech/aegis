"""Raphael's topic tools (#513). `track_topic` is still hand-written in
`chat.py` (its schema predates the registry); `untrack_topic` lives here. The
logic is `services/research_topics.py`, shared with the curiosity hook."""

from __future__ import annotations

import json

import asyncpg

from aegis.services import research_topics
from aegis.services.tools.base import ToolContext
from aegis.services.tools.registry import aegis_tool


@aegis_tool
async def _exec_untrack_topic(pool: asyncpg.Pool, ctx: ToolContext, *, topic_name: str) -> str:
    """Stop tracking a topic: the scans and feeds stop collecting for it, and its open round of news closes (its task, if it had one, is closed with a note).

    Args:
        topic_name: The topic's name, as track_topic was given it.
    """
    try:
        return json.dumps(await research_topics.untrack(pool, topic_name), default=str)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
