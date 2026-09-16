"""Document-attachment chat tools — YouTube transcript, PDF → text.

Both fetch a long document and hand it to the comms delivery server as a text
attachment rather than returning it: a transcript or a paper is orders of
magnitude past what belongs in a prompt, so the tool result is a confirmation
plus a short preview.
"""

from __future__ import annotations

import json

import asyncpg

from aegis.errors import error_text
from aegis.services.tools.base import ToolContext
from aegis.services.tools.registry import aegis_tool


async def _deliver_documents(ctx: ToolContext, documents: list[dict], caption: str) -> dict:
    """POST text attachments to the comms delivery server (/api/deliver/document).

    Targets the channel the user's message came from (chat_context.delivery_ref)
    when known; otherwise comms falls back to the agent's bound channel.
    """
    comms_url = (getattr(ctx.settings, "comms_url", "") or "").rstrip("/")
    if not comms_url:
        return {"ok": False, "error": "comms_url not configured"}
    import httpx

    api_key = getattr(ctx.settings, "api_key", "") or ""
    headers = {"X-API-Key": api_key} if api_key else {}
    ref = (ctx.chat_context or {}).get("delivery_ref") or {}
    body = {
        "documents": documents,
        "caption": caption,
        # "" lets comms pick its default (the gtd holder), never an example id (#579).
        "agent_id": ctx.agent_id or "",
        "target": {"channel": ref["channel"]} if ref.get("channel") else None,
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{comms_url}/api/deliver/document", json=body, headers=headers
            )
        if resp.status_code == 200 and (resp.json() or {}).get("ok"):
            return {"ok": True}
        return {"ok": False, "error": f"comms status {resp.status_code}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": error_text(exc)}


@aegis_tool
async def _exec_youtube_transcript(pool: asyncpg.Pool, ctx: ToolContext, *, url: str) -> str:
    """Fetch the caption transcript of a YouTube video and deliver it to the user's channel as a text-file attachment. Returns a short confirmation with a preview — the full transcript is in the attachment, so do NOT try to reproduce it in your reply.

    Args:
        url: The YouTube video URL
    """
    from aegis.services.content_extract import extract_youtube_id, fetch_youtube_transcript

    url = (url or "").strip()
    video_id = extract_youtube_id(url)
    if not video_id:
        return json.dumps({"error": "Not a recognizable YouTube URL"})
    text, meta = await fetch_youtube_transcript(url)
    if not text:
        return json.dumps(
            {"error": "No transcript available (video has no captions or the fetch failed)"}
        )
    delivery = await _deliver_documents(
        ctx,
        documents=[{"filename": f"youtube-{video_id}-transcript.txt", "content": text}],
        caption=f"Transcript for {url}",
    )
    if not delivery.get("ok"):
        return json.dumps(
            {"error": f"Transcript fetched but delivery failed: {delivery.get('error')}"}
        )
    return json.dumps(
        {
            "ok": True,
            "video_id": video_id,
            "segments": meta.get("segments"),
            "words": len(text.split()),
            "note": "Full transcript delivered to the channel as a file attachment.",
            "preview": text[:300],
        }
    )


@aegis_tool
async def _exec_pdf_to_text(pool: asyncpg.Pool, ctx: ToolContext, *, url: str) -> str:
    """Download a PDF from a URL, extract its text, and deliver it to the user's channel as a text-file attachment. Returns a short confirmation with a preview — the full text is in the attachment, so do NOT try to reproduce it in your reply.

    Args:
        url: Direct http(s) URL to the PDF
    """
    from pathlib import PurePosixPath
    from urllib.parse import urlparse

    from aegis.services.content_extract import fetch_and_extract
    from aegis.services.url_guard import UnsafeURLError

    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return json.dumps({"error": "A full http(s) URL to a PDF is required"})
    try:
        text, _title = await fetch_and_extract(url, max_chars=2_000_000)
    except UnsafeURLError as exc:
        return json.dumps({"error": f"That URL cannot be fetched: {exc}"})
    if not text:
        return json.dumps(
            {"error": "Could not extract text (fetch failed, not a PDF, or scanned/image-only)"}
        )
    stem = PurePosixPath(urlparse(url).path).stem or "document"
    delivery = await _deliver_documents(
        ctx,
        documents=[{"filename": f"{stem}.txt", "content": text}],
        caption=f"Extracted text from {url}",
    )
    if not delivery.get("ok"):
        return json.dumps(
            {"error": f"Text extracted but delivery failed: {delivery.get('error')}"}
        )
    return json.dumps(
        {
            "ok": True,
            "chars": len(text),
            "note": "Full text delivered to the channel as a file attachment.",
            "preview": text[:300],
        }
    )
