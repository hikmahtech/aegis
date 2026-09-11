"""Raphael's feed tools (#511): see the feeds, add one, drop one.

The feed list is `channels(kind='rss')` and AEGIS owns it, so "Raphael, stop
following X" is a chat request rather than an admin-page errand. The logic is
`services/feeds.py`, shared with the admin route and the worker.
"""

from __future__ import annotations

import json

import asyncpg

from aegis.services import feeds
from aegis.services.tools.base import ToolContext
from aegis.services.tools.registry import aegis_tool

# What `list_feeds` shows per feed. The admin page shows the rest.
_LIST_KEYS = (
    "label",
    "identifier",
    "active",
    "ingest",
    "entries_30d",
    "stored_30d",
    "abstract_30d",
    "used_30d",
    "used_90d",
    "last_entry_at",
    "fetch_failures",
    "last_fetch_error",
    "tracking_since",
)


@aegis_tool
async def _exec_list_feeds(pool: asyncpg.Pool, ctx: ToolContext) -> str:
    """List the RSS feeds Raphael follows and what each is worth: entries and stored documents in the last 30 days, how many of its documents a prompt used in the last 30 and 90 days, its ingest mode, its last entry and any fetch failures."""
    rows = await feeds.feed_stats(pool)
    return json.dumps(
        {
            "feeds": [{k: f.get(k) for k in _LIST_KEYS} for f in rows],
            "note": "used = a document from the feed was put into a chat prompt",
        },
        default=str,
    )


# The executor is `_exec_follow_feed` because the n8n-era executor for a tool
# of this name was deleted, and `ci-grep-guard.yml` fails any tree that still
# carries that old symbol. The tool itself is `subscribe_feed` (#511).
@aegis_tool(name="subscribe_feed")
async def _exec_follow_feed(
    pool: asyncpg.Pool, ctx: ToolContext, *, url: str, label: str = ""
) -> str:
    """Follow an RSS or Atom feed. The URL is fetched first and must be a feed; a web page that advertises a feed returns that feed's URL to try instead.

    Args:
        url: The feed's URL.
        label: A short name for the feed. Defaults to the feed's own title.
    """
    return json.dumps(
        await feeds.subscribe(pool, url, label=label, agent_id=ctx.agent_id or None)
    )


@aegis_tool
async def _exec_unsubscribe_feed(pool: asyncpg.Pool, ctx: ToolContext, *, feed: str) -> str:
    """Stop following an RSS feed. Its history is kept, and following it again resumes it.

    Args:
        feed: The feed's URL or its label, as list_feeds shows it.
    """
    return json.dumps(await feeds.unsubscribe(pool, feed))
