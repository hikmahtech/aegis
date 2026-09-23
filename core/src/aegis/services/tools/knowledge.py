"""Knowledge chat tools — semantic search, Q&A, and remember-this ingest.

All three ride `ctx.knowledge_connector`; a missing connector is reported as an
explicit "unavailable" status, never as an empty result set.
"""

from __future__ import annotations

import json
import time

import asyncpg
import structlog

from aegis.errors import error_text
from aegis.services.knowledge_ranking import get_ranking
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
        logger.warning("search_knowledge_unreachable", error=error_text(exc, 500))
        return _knowledge_unavailable(f"search failed: {exc}")
    return json.dumps(results, default=_json_default)


@aegis_tool
async def _exec_ask_knowledge(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    question: str,
    source_type: str | None = None,
    tags: list[str] | None = None,
    since_days: int | None = None,
) -> str:
    """Get a short answer to a factual question from the knowledge base, written from the best few documents and citing them as [n], with each source's title, url, source_type and date. Use this when you want a fact or a summary (what did the bank mail say about the card limit, what did last week's meeting decide). Use search_knowledge instead to browse, or when you need the documents themselves. Narrow the question with source_type, tags or since_days: the store is mostly research papers, so an unfiltered question about mail or meetings can miss. If answered is false, nothing in the knowledge base matched.

    Args:
        question: The question, in plain words
        source_type: Only documents of this type, e.g. email, meeting, note, research, chat
        tags: Only documents carrying at least one of these tags
        since_days: Only documents added in the last this many days
    """
    if not ctx.knowledge_connector:
        return _knowledge_unavailable()
    domains: list[str] | None = None
    if ctx.agent_id:
        try:
            meta = await pool.fetchval("SELECT metadata FROM agents WHERE id = $1", ctx.agent_id)
            domains = (meta or {}).get("knowledge_domains") if isinstance(meta, dict) else None
        except Exception as exc:  # a missing boost must not cost the answer
            logger.warning("ask_knowledge_domains_unread", error=error_text(exc, 300))
    try:
        result = await ctx.knowledge_connector.ask(
            question,
            source_type=source_type,
            tags=tags,
            since_days=since_days,
            ranking=await get_ranking(pool),
            knowledge_domains=domains,
            agent_id=ctx.agent_id,
        )
    except Exception as exc:
        logger.warning("ask_knowledge_unreachable", error=error_text(exc, 500))
        return _knowledge_unavailable(f"ask failed: {exc}")
    result["answered"] = bool(result.get("sources"))
    if not result["answered"]:
        result["answer"] = "The knowledge base has no documents that answer this."
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
        logger.warning("remember_this_failed", error=error_text(exc, 500))
        return json.dumps({"stored": False, "error": error_text(exc, 500)})
