"""The research agent's research tools (#509).

Four read-only tools over the shared steps in `services/research.py` —
`web_search`, `read_url`, `paper_search`, `paper_read` — and `research_topic`,
which does not research here at all: it hands the question to `ResearchFlow`
and waits a short while for the answer.

Why the hand-off: a research run is several searches, a few page reads and a
model call. That does not fit the chat loop's per-tool budget, and the chat
loop cannot cancel it either, so doing it here could only misreport a slow run.
The flow runs under an id derived from the question, so a retried turn — or a
model re-asking what it believes timed out — attaches to the run already in
flight, and one that outlasts the wait posts its answer to the agent's channel
itself. It is the seam `ledger.py` uses for books writes (#388).
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Literal

import asyncpg
import structlog
from pydantic import Field

from aegis.errors import error_text
from aegis.services import research as rs
from aegis.services import research_config
from aegis.services.agents import resolve_tag
from aegis.services.tools.base import ToolContext
from aegis.services.tools.registry import aegis_tool

logger = structlog.get_logger()


def _s2_key(ctx: ToolContext) -> str:
    return str(getattr(ctx.settings, "semantic_scholar_api_key", "") or "")


@aegis_tool
async def _exec_web_search(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    query: str,
    limit: Annotated[int, Field(ge=1, le=20)] = 8,
    site: str = "",
) -> str:
    """Search the web and return the raw results — title, url and a snippet for each — without summarising them. Read a promising result with read_url.

    Args:
        query: What to search for.
        limit: How many results (1-20).
        site: Only results from this domain, e.g. arxiv.org.
    """
    if ctx.search_connector is None:
        return json.dumps({"error": "web search is not configured (no SearxNG URL)"})
    query = (query or "").strip()
    if not query:
        return json.dumps({"error": "query is required"})
    try:
        results = await rs.web_search(
            ctx.search_connector, query, limit=max(1, min(int(limit), 20)), site=site
        )
    except Exception as exc:  # noqa: BLE001 — a failed search is an answer, not a crash
        logger.warning("web_search_failed", error=error_text(exc))
        return json.dumps({"error": f"web search failed: {error_text(exc)}"})
    return json.dumps({"query": query, "results": results})


@aegis_tool
async def _exec_read_url(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    url: str,
    max_chars: Annotated[int, Field(ge=500, le=60000)] = rs.READ_URL_CHARS,
) -> str:
    """Read one web page and return its readable text. Nothing is saved. Only public http(s) pages can be read.

    Args:
        url: The page to read.
        max_chars: The most characters of text to return (500-60000).
    """
    return json.dumps(await rs.read_url(url, max_chars=max_chars))


@aegis_tool
async def _exec_paper_search(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    query: str,
    since: str = "",
    limit: Annotated[int, Field(ge=1, le=25)] = 8,
) -> str:
    """Find academic papers on arXiv and Semantic Scholar: title, authors, date, abstract, citation count, and an id to pass to paper_read. Nothing is saved.

    Args:
        query: What the papers are about.
        since: Only papers published on or after this date: YYYY, YYYY-MM or YYYY-MM-DD.
        limit: How many papers (1-25).
    """
    return json.dumps(
        await rs.paper_search(query, since=since, limit=limit, api_key=_s2_key(ctx))
    )


@aegis_tool
async def _exec_paper_read(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    paper_id: str,
    max_chars: Annotated[int, Field(ge=500, le=60000)] = rs.PAPER_READ_CHARS,
) -> str:
    """Read a paper's text from its PDF. Nothing is saved.

    Args:
        paper_id: An arXiv id (2401.01234), an id from paper_search (arxiv:… or s2:…), or a PDF URL.
        max_chars: The most characters of text to return (500-60000).
    """
    return json.dumps(await rs.paper_read(paper_id, max_chars=max_chars, api_key=_s2_key(ctx)))


@aegis_tool
async def _exec_research_topic(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    query: str,
    depth: Literal["quick", "thorough"] | None = None,
    domains: list[str] | None = None,
) -> str:
    """Research a question: search the knowledge store, the web and (for academic questions) papers, read the best sources, and answer with numbered citations. Runs in the background as a research flow and waits a short while for it; a longer run posts its answer to the channel when it is ready. The answer is saved, replacing any earlier answer to the same question.

    Args:
        query: What to research
        depth: Search depth (default: quick)
        domains: Limit web search to specific domains

    Returns:
        Never raises. Three outcomes: the run finished inside the wait and its
        answer comes back; it did not, and the model is told it is still running
        (the flow posts the answer to the agent's channel when it lands); or it
        could not be started at all, in which case nothing ran.
    """
    from temporalio.exceptions import WorkflowAlreadyStartedError

    question = str(query or "").strip()
    if not question:
        return json.dumps({"error": "query is required"})
    depth = depth if depth in rs.DEPTHS else "quick"
    domains = rs.clean_domains(domains)
    client = ctx.temporal_client
    if client is None:
        return json.dumps(
            {
                "error": "research could not be started — Temporal is not reachable. "
                "Nothing ran; try again once it is back."
            }
        )
    # The run belongs to the calling agent, else to whoever holds the
    # `research` tag; a deployment with no such agent still gets its answer,
    # the flow just has nobody's channel to post a late one to.
    agent_id = ctx.agent_id or ""
    if not agent_id and pool is not None:
        agent_id = await resolve_tag(pool, "research") or ""
    wait_s = int((await research_config.get_research_config(pool))["wait_seconds"])
    workflow_id = rs.research_workflow_id(question, depth, domains)
    reattached = False
    try:
        handle = await client.start_workflow(
            rs.RESEARCH_FLOW,
            {
                "agent_id": agent_id,
                "question": question,
                "depth": depth,
                "domains": domains,
                "reply_after_seconds": wait_s,
            },
            id=workflow_id,
            task_queue=rs.TASK_QUEUE,
        )
    except WorkflowAlreadyStartedError:
        reattached = True
        handle = client.get_workflow_handle(workflow_id)
    except Exception as exc:  # noqa: BLE001 — a dispatch failure is an answer, not a crash
        logger.warning("research_dispatch_failed", error=error_text(exc))
        return json.dumps({"error": f"research could not be started: {error_text(exc)}"})
    try:
        result = await asyncio.wait_for(handle.result(), timeout=wait_s)
    except TimeoutError:
        # Cancelling `handle.result()` stops the WAIT, not the research: the
        # run carries on and posts its answer to the agent's channel.
        logger.info("research_still_running", workflow_id=workflow_id, reattached=reattached)
        return json.dumps(
            {
                "status": "running",
                "workflow_id": workflow_id,
                "message": f"Still researching after {wait_s}s. The answer will "
                "be posted to this channel when it is ready. Do not start it again.",
            }
        )
    except Exception as exc:  # noqa: BLE001 — the run failed; say so, don't raise
        logger.warning("research_failed", workflow_id=workflow_id, error=error_text(exc))
        return json.dumps(
            {"error": f"research failed: {error_text(exc)}", "workflow_id": workflow_id}
        )
    result = result if isinstance(result, dict) else {}
    sources = [s for s in (result.get("sources") or []) if isinstance(s, dict)]
    return json.dumps(
        {
            "synthesis": result.get("answer") or "",
            "sources": sources,
            "top_urls": [s["url"] for s in sources if str(s.get("url") or "").startswith("http")][:5],
            "saved": bool(result.get("saved")),
            "workflow_id": workflow_id,
            "reattached": reattached,
        },
        default=str,
    )
