"""Raphael's topic tools (#513). The logic is `services/research_topics.py`,
shared with the curiosity hook that asks "track this?"."""

from __future__ import annotations

import json
from typing import Literal

import asyncpg

from aegis.services import research_topics
from aegis.services.tools.base import ToolContext
from aegis.services.tools.registry import aegis_tool


@aegis_tool
async def _exec_track_topic(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    topic_name: str,
    queries: list[str],
    priority: Literal["high", "medium", "low"] | None = None,
) -> str:
    """Subscribe to ongoing intelligence monitoring for a topic. AEGIS will periodically scan news sources and include findings in daily briefings.

    Args:
        topic_name: Name for this topic
        queries: Search terms for this topic
        priority: Monitoring priority (default: medium)
    """
    try:
        out = await research_topics.track(
            pool,
            str(topic_name or ""),
            list(queries or []),
            str(priority or "medium"),
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps(out, default=str)


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
