"""Raphael's notes tools (#514): the Obsidian vault, read and appended to.

Two reads — `note_search` over the index, `note_read` of one note from the
checkout — and two writes, `note_write` and `note_link`, which never write here:
they validate, hand the write to `NotesWriteFlow` under an id derived from its
own content and wait a short while, the seam `ledger.py` uses for the books
(#388). A retried turn re-attaches to the write in flight, and one that
outlasts the wait reports itself to the agent's channel.

Every write is append-only and lands under `raphael/`; the rules live in
`services/notes.py`, which is the only thing that touches the vault.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

import asyncpg
import structlog
from pydantic import Field

from aegis.services import notes
from aegis.services import notes_write as nw
from aegis.services.tools.base import ToolContext
from aegis.services.tools.registry import aegis_tool
from aegis.services.user_time import user_now

logger = structlog.get_logger()

# How long a write tool waits for `NotesWriteFlow` before telling the model it
# is still running. The fast path is a pull, an append, a commit and a push —
# a few GitHub round trips — so this is the books' 20s for the same reason.
NOTES_WRITE_WAIT_S = 20
# The chat loop's per-tool floor under that wait.
NOTES_TOOL_TIMEOUT_S = NOTES_WRITE_WAIT_S + 10
# A read may pull the vault first.
NOTE_READ_TIMEOUT_S = 60

_NOTES_WRITE_FLOW = "NotesWriteFlow"
_TASK_QUEUE = "aegis-main"

_NOT_CONFIGURED = (
    "the vault is not configured (notes_repo_url and notes_deploy_key on the "
    "Integrations page)"
)


async def _dispatch_notes_write(ctx: ToolContext, op: str, payload: dict) -> str:
    """Hand a validated write to `NotesWriteFlow` and wait a short while for it.

    Returns the sentence the model relays; never raises. There is no in-process
    fallback when Temporal is unreachable — the chat loop cannot cancel a git
    write, which is the whole reason for the flow.
    """
    from temporalio.exceptions import WorkflowAlreadyStartedError

    client = ctx.temporal_client
    if client is None:
        return (
            "error: the vault write could not be queued — Temporal is not reachable. "
            "Nothing was written."
        )
    workflow_id = nw.write_workflow_id(op, payload)
    reattached = False
    try:
        handle = await client.start_workflow(
            _NOTES_WRITE_FLOW,
            {
                "agent_id": ctx.agent_id or "raphael",
                "op": op,
                "payload": payload,
                "reply_after_seconds": NOTES_WRITE_WAIT_S,
            },
            id=workflow_id,
            task_queue=_TASK_QUEUE,
        )
    except WorkflowAlreadyStartedError:
        reattached = True
        handle = client.get_workflow_handle(workflow_id)
    except Exception as exc:  # noqa: BLE001 — a dispatch failure is an answer, not a crash
        logger.warning("notes_write_dispatch_failed", op=op, error=str(exc)[:200])
        return f"error: the vault write could not be queued: {str(exc)[:200]}. Nothing was written."
    try:
        result = await asyncio.wait_for(handle.result(), timeout=NOTES_WRITE_WAIT_S)
    except TimeoutError:
        logger.info(
            "notes_write_still_running", op=op, workflow_id=workflow_id, reattached=reattached
        )
        return (
            f"the vault write is still running as {workflow_id} — longer than "
            f"{NOTES_WRITE_WAIT_S}s. It will finish on its own and report the outcome "
            "here. Do not run it again."
        )
    except Exception as exc:  # noqa: BLE001 — the workflow failed; say so, don't raise
        logger.warning("notes_write_failed", op=op, workflow_id=workflow_id, error=str(exc)[:200])
        return f"error: the vault write failed: {str(exc)[:200]}"
    return nw.describe_result(result)


@aegis_tool
async def _exec_note_search(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    query: str,
    limit: Annotated[int, Field(ge=1, le=20)] = 5,
) -> str:
    """Search the user's Obsidian vault — journal, knowledge, literature and reference notes, and Raphael's own notes — and return the best-matching notes with their paths. Read one in full with note_read.

    Args:
        query: What to look for.
        limit: How many notes (1-20).
    """
    if ctx.knowledge_connector is None:
        return json.dumps({"error": "the knowledge store is not available"})
    query = (query or "").strip()
    if not query:
        return json.dumps({"error": "query is required"})
    try:
        hits = await ctx.knowledge_connector.search(
            query, limit=max(1, min(int(limit), 20)), source_type="note"
        )
    except Exception as exc:  # noqa: BLE001 — a failed search is an answer, not a crash
        logger.warning("note_search_failed", error=str(exc)[:200])
        return json.dumps({"error": f"note search failed: {str(exc)[:200]}"})
    notes_found = [
        {
            "path": notes.note_path_from_url(str(h.get("url") or "")),
            "title": h.get("title"),
            "similarity": round(float(h.get("similarity") or 0.0), 3),
            "snippet": str(h.get("content") or h.get("chunk_text") or h.get("summary") or "")[
                :600
            ],
        }
        for h in hits or []
    ]
    return json.dumps({"query": query, "notes": notes_found})


@aegis_tool
async def _exec_note_read(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    path: str,
    max_chars: Annotated[int, Field(ge=500, le=60000)] = 20000,
) -> str:
    """Read one note from the user's Obsidian vault by its path, e.g. journal/2026/09. Sep/12 Sep 26.md. Encrypted blocks are never shown.

    Args:
        path: The note's path inside the vault.
        max_chars: The most characters to return (500-60000).
    """
    cfg = notes.config_from_settings(ctx.settings)
    if not cfg.configured:
        return json.dumps({"error": _NOT_CONFIGURED})
    return json.dumps(await notes.read_note(cfg, (path or "").strip(), max_chars))


@aegis_tool
async def _exec_note_write(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    path: str,
    text: str,
    heading: str = "",
    title: str = "",
) -> str:
    """Add a section to one of Raphael's notes in the vault, creating the note if it does not exist. Append-only: nothing already in a note is ever changed. Only notes under raphael/ can be written, e.g. raphael/topics/rag.md.

    Args:
        path: The note, under raphael/ (the folder and .md are added if left off).
        text: The markdown to add.
        heading: The section heading; today's date when empty.
        title: The note's title if this creates it.
    """
    if not notes.config_from_settings(ctx.settings).configured:
        return f"error: {_NOT_CONFIGURED}. Nothing was written."
    # An empty heading becomes today's date: the user's today (`user_timezone`),
    # not the container's UTC one.
    payload, problem = nw.normalise("write", {"path": path, "text": text, "heading": heading,
                                              "title": title}, await user_now(pool))
    if problem:
        return f"error: {problem}. Nothing was written."
    return await _dispatch_notes_write(ctx, "write", payload)


@aegis_tool
async def _exec_note_link(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    path: str,
    target: str,
    label: str = "",
) -> str:
    """Link one of Raphael's notes to another note, a book, a paper or a URL by appending one line: a [[wikilink]] for a note or book title, a markdown link for a URL. Append-only, under raphael/ only.

    Args:
        path: The note to add the link to, under raphael/.
        target: A note or book title (becomes [[target]]) or an http(s) URL.
        label: Optional text to show instead of the target.
    """
    if not notes.config_from_settings(ctx.settings).configured:
        return f"error: {_NOT_CONFIGURED}. Nothing was written."
    payload, problem = nw.normalise("link", {"path": path, "target": target, "label": label})
    if problem:
        return f"error: {problem}. Nothing was written."
    return await _dispatch_notes_write(ctx, "link", payload)
