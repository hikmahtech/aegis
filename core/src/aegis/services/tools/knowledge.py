"""Knowledge chat tools — semantic search, Q&A, and remember-this ingest.

All three ride `ctx.knowledge_connector`; a missing connector is reported as an
explicit "unavailable" status, never as an empty result set.
"""

from __future__ import annotations

import json
import time

import asyncpg
import structlog

from aegis.services.tools.base import ToolContext, _json_default
from aegis.services.tools.registry import aegis_tool

logger = structlog.get_logger()


def _knowledge_unavailable(detail: str = "Knowledge service not available") -> str:
    """Return a clearly-labeled 'service down' status.

    Distinct from an empty successful search so the LLM can decide whether to
    retry, apologise to the user, or fall back to another tool instead of
    treating the gap as "no results found".
    """
    return json.dumps({"status": "unavailable", "error": detail, "retry_suggested": True})


@aegis_tool
async def _exec_search_knowledge(
    pool: asyncpg.Pool, ctx: ToolContext, *, query: str, limit: int = 10
) -> str:
    """Search the knowledge base using semantic similarity. Returns relevant content with titles, summaries, and similarity scores.

    Args:
        query: Natural language search query
        limit: Max results (1-100)
    """
    if not ctx.knowledge_connector:
        return _knowledge_unavailable()
    try:
        results = await ctx.knowledge_connector.search(query, limit=limit)
    except Exception as exc:
        logger.warning("search_knowledge_unreachable", error=str(exc))
        return _knowledge_unavailable(f"search failed: {exc}")
    return json.dumps(results, default=_json_default)


@aegis_tool
async def _exec_ask_knowledge(pool: asyncpg.Pool, ctx: ToolContext, *, question: str) -> str:
    """Ask a question and get a synthesized answer from the knowledge base with sources and confidence scores.

    Args:
        question: Natural language question
    """
    if not ctx.knowledge_connector:
        return _knowledge_unavailable()
    try:
        result = await ctx.knowledge_connector.ask(question)
    except Exception as exc:
        logger.warning("ask_knowledge_unreachable", error=str(exc))
        return _knowledge_unavailable(f"ask failed: {exc}")
    return json.dumps(result, default=_json_default)


@aegis_tool
async def _exec_remember_this(
    pool: asyncpg.Pool, ctx: ToolContext, *, summary: str, tags: list[str] | None = None
) -> str:
    """Store important information from this conversation in the knowledge base for future reference. Only call when something is worth remembering long-term.

    Args:
        summary: Concise summary of what to remember
        tags: Categorization tags
    """
    if not ctx.knowledge_connector:
        return json.dumps({"error": "Knowledge service not available"})
    chat_ctx = ctx.chat_context or {}
    thread_id = chat_ctx.get("thread_id", "unknown")
    timestamp = int(time.time())
    raw_text = f"User: {chat_ctx.get('user_message', '')}\nSummary: {summary}"
    try:
        result = await ctx.knowledge_connector.ingest_content(
            url=f"aegis://chat/{thread_id}/{timestamp}",
            title=summary,
            summary=summary,
            source_type="chat",
            raw_text=raw_text,
            tags=tags or [],
        )
        return json.dumps({"stored": True, **result}, default=str)
    except Exception as exc:
        logger.warning("remember_this_failed", error=str(exc))
        return json.dumps({"stored": False, "error": str(exc)})
