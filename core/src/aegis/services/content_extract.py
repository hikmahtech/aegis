"""Fetch + extract readable text from URLs and uploaded files.

This restores the content-extraction the external knowledge-service used to do
server-side, now in-process. Used by:
  - the knowledge ingest/upload/folder routes (UI seeding), and
  - the worker content activities (automated article/PDF ingest flows),
so a URL or file becomes plain text before it hits KnowledgeStore.ingest_content.

ponytail: trafilatura for HTML (purpose-built, one dep) + pdfminer (already a
dep) for PDF. No OCR for images — they return empty text and the caller keeps
title/metadata. docx is not handled (add python-docx if a real need shows up).
"""

from __future__ import annotations

import asyncio
import io
import re
from pathlib import PurePosixPath

import httpx
import structlog

logger = structlog.get_logger()

_MAX_BYTES = 10 * 1024 * 1024  # 10 MB fetch cap
_MAX_TEXT = 100_000  # cap stored text (matches the worker's old KS cap)


def extract_html(html: str) -> tuple[str, str | None]:
    """Readable article text + title from raw HTML. ('', None) if nothing useful."""
    if not html:
        return "", None
    import trafilatura

    text = trafilatura.extract(html, include_comments=False, include_tables=True) or ""
    title: str | None = None
    try:
        meta = trafilatura.extract_metadata(html)
        title = getattr(meta, "title", None) if meta else None
    except Exception:  # noqa: BLE001 — metadata is best-effort
        title = None
    return text[:_MAX_TEXT].strip(), title


def extract_pdf(data: bytes, max_chars: int = _MAX_TEXT) -> str:
    """Text from a PDF byte string. '' on failure."""
    try:
        from pdfminer.high_level import extract_text

        return (extract_text(io.BytesIO(data)) or "")[:max_chars].strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("pdf_extract_failed", error=str(exc)[:200])
        return ""


def extract_bytes(
    data: bytes, content_type: str = "", filename: str = ""
) -> tuple[str, str | None]:
    """Dispatch raw bytes (an upload) to the right extractor by extension/type.

    Returns (text, title). Title is only derived for HTML.
    """
    ext = PurePosixPath(filename).suffix.lower()
    ct = (content_type or "").lower()
    if ext == ".pdf" or "pdf" in ct:
        return extract_pdf(data), None
    if ext in (".html", ".htm") or "html" in ct:
        return extract_html(data.decode("utf-8", errors="replace"))
    # txt, md, json, csv, or anything else text-ish: decode as-is.
    return data.decode("utf-8", errors="replace")[:_MAX_TEXT].strip(), None


async def fetch_and_extract(
    url: str, content_type: str | None = None, max_chars: int = _MAX_TEXT
) -> tuple[str, str | None]:
    """GET a URL and extract readable text. Returns (text, title).

    `content_type` is an optional caller hint ('pdf'/'article'/'image'/...);
    the response's own Content-Type header takes precedence. Images (no OCR)
    and fetch failures return ('', None) so the caller can fall back to a
    summary/title.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content[:_MAX_BYTES]
            ct = resp.headers.get("content-type", "").lower()
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_failed", url=url[:200], error=str(exc)[:200])
        return "", None

    # %PDF- magic-byte sniff catches PDFs served as octet-stream / mislabeled.
    if "pdf" in ct or content_type == "pdf" or data[:5] == b"%PDF-":
        return extract_pdf(data, max_chars=max_chars), None
    if content_type == "image" or ct.startswith("image/"):
        return "", None  # no OCR
    decoded = data.decode("utf-8", errors="replace")
    if "html" in ct:
        return extract_html(decoded)
    if ct.startswith("text/"):  # text/plain, text/markdown, … — use as-is
        return decoded[:max_chars].strip(), None
    # No/unknown Content-Type (or an article hint): best-effort HTML extract,
    # falling back to the raw decoded body if it wasn't HTML.
    text, title = extract_html(decoded)
    if text:
        return text, title
    return decoded[:max_chars].strip(), None


# --- YouTube transcripts ---

_YOUTUBE_ID_RE = re.compile(r"(?:v=|youtu\.be/|embed/|shorts/|live/)([a-zA-Z0-9_-]{11})")


def extract_youtube_id(url: str) -> str | None:
    """Extract the 11-char YouTube video id from a URL, or None."""
    m = _YOUTUBE_ID_RE.search(url or "")
    return m.group(1) if m else None


async def fetch_youtube_transcript(url: str) -> tuple[str, dict]:
    """Full caption transcript for a YouTube URL via youtube-transcript-api.

    Returns (text, {"video_id", "segments"}); ('', {...}) when the URL isn't
    YouTube, the video has no captions, or the fetch fails (logged).
    """
    video_id = extract_youtube_id(url)
    if not video_id:
        return "", {}
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        transcript = await asyncio.to_thread(YouTubeTranscriptApi().fetch, video_id)
        text = " ".join(snippet.text for snippet in transcript).strip()
        return text, {"video_id": video_id, "segments": len(transcript)}
    except Exception as exc:  # noqa: BLE001 — no captions / blocked / API change
        logger.warning("youtube_transcript_failed", video_id=video_id, error=str(exc)[:200])
        return "", {"video_id": video_id}
