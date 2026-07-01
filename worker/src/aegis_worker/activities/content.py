"""Content extraction activities for the intelligence pipeline.

Fetches and extracts readable text from URLs in-process (articles via
trafilatura, PDFs via pdfminer) before handing it to the knowledge subsystem.
Media (YouTube captions / ElevenLabs Scribe) is transcribed locally. Images
have no local OCR, so an image URL with no caller-supplied excerpt is skipped.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
import structlog
from aegis.services.content_extract import extract_html, fetch_and_extract
from temporalio import activity

logger = structlog.get_logger()

# --- Content type detection ---

_MEDIA_DOMAINS = {"youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com"}
_MEDIA_EXTENSIONS = {".mp3", ".mp4", ".m4a", ".wav", ".ogg", ".webm", ".flac", ".aac", ".mpeg"}
_PDF_EXTENSIONS = {".pdf"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_ARXIV_ABS_RE = re.compile(r"arxiv\.org/abs/")
# Captures the paper ID after /abs/ — letters, digits, dot, slash (for old
# pre-2007 IDs like "math/0211159"). Stops at any other character so version
# suffixes ("v2"), trailing slashes, or fragments don't pollute the rewrite.
_ARXIV_ABS_ID_RE = re.compile(r"arxiv\.org/abs/([\w./-]+)")


def rewrite_arxiv_abs_to_pdf(url: str) -> str:
    """Rewrite ``https://arxiv.org/abs/<id>`` → ``https://arxiv.org/pdf/<id>``.

    The ``/abs/`` page is a thin HTML wrapper around the paper's abstract
    (~500 chars). Sending it to knowledge-service produces a single-chunk
    document that the LLM extractor cannot pull triples from — observed in
    the 2026-05-26 prod audit: 154 of 200 jobs were arxiv items with zero
    triples emitted (knowledge-service PR #83 description).

    The ``/pdf/`` URL serves the actual paper. knowledge-service's
    PdfParser (PyMuPDF) handles it and chunks the full text, giving the
    extractor real material to work with.

    Unrecognised arxiv URL shapes pass through unchanged.
    """
    match = _ARXIV_ABS_ID_RE.search(url)
    if not match:
        return url
    paper_id = match.group(1).rstrip("/")
    # ``.pdf`` suffix so ``detect_content_type`` keeps classifying as PDF —
    # arxiv serves the same bytes whether or not the extension is present,
    # but the classifier looks for ``.pdf``.
    return f"https://arxiv.org/pdf/{paper_id}.pdf"


def detect_content_type(url: str) -> str:
    """Detect content type from URL pattern.

    Returns: 'article', 'pdf', 'image', or 'media'.
    """
    parsed = urlparse(url)
    domain = parsed.netloc.lower()

    if domain in _MEDIA_DOMAINS:
        return "media"

    if _ARXIV_ABS_RE.search(url):
        return "pdf"

    path = parsed.path.lower()
    query = parsed.query.lower()
    for ext in _MEDIA_EXTENSIONS:
        if path.endswith(ext):
            return "media"
    for ext in _PDF_EXTENSIONS:
        if path.endswith(ext):
            return "pdf"
    # Query-param or Raindrop file endpoint detection
    if "type=application/pdf" in query or "type=application%2fpdf" in query:
        return "pdf"
    if domain == "api.raindrop.io" and path.endswith("/file"):
        return "pdf"
    for ext in _IMAGE_EXTENSIONS:
        if path.endswith(ext):
            return "image"

    return "article"


# --- Constants ---

_MIN_CONTENT_LENGTH = 200
_USER_AGENT = "Mozilla/5.0 (compatible; AegisBot/2.0; +https://aegis.example.com)"


@dataclass
class ContentResult:
    """Result of content extraction."""

    text: str
    title: str
    word_count: int
    extraction_method: str
    metadata: dict = field(default_factory=dict)


# --- Domain hints mapping ---

_DOMAIN_MAP = {
    "technology": ["technology", "infrastructure"],
    "sentry": ["technology", "infrastructure"],
    "finance": ["finance"],
    "economics": ["finance"],
    "research": ["science"],
    "academic": ["science"],
    "news": ["news"],
}


def _category_to_domains(category: str) -> list[str]:
    """Map triage category to knowledge-service domain hints."""
    return _DOMAIN_MAP.get(category, [])


# --- File download helper (for media) ---


async def _download_file(
    client: httpx.AsyncClient,
    url: str,
    max_bytes: int,
    suffix: str,
    extra_headers: dict[str, str] | None = None,
) -> str | None:
    """Download URL to temp file. Returns temp path or None on failure."""
    try:
        headers = {"User-Agent": _USER_AGENT}
        if extra_headers:
            headers.update(extra_headers)
        async with client.stream(
            "GET", url, headers=headers, follow_redirects=True, timeout=60
        ) as resp:
            resp.raise_for_status()
            total = 0
            fd, path = tempfile.mkstemp(suffix=suffix)
            try:
                with os.fdopen(fd, "wb") as f:
                    async for chunk in resp.aiter_bytes(8192):
                        total += len(chunk)
                        if total > max_bytes:
                            os.unlink(path)
                            return None
                        f.write(chunk)
            except Exception:
                if os.path.exists(path):
                    os.unlink(path)
                raise
            return path
    except Exception as exc:
        logger.warning("download_failed", url=url, error=str(exc))
        return None


# --- Media transcription ---

_YOUTUBE_ID_RE = re.compile(r"(?:v=|youtu\.be/|embed/)([a-zA-Z0-9_-]{11})")
_MAX_MEDIA_BYTES = 500 * 1024 * 1024  # 500MB


def _extract_youtube_id(url: str) -> str | None:
    """Extract YouTube video ID from URL."""
    m = _YOUTUBE_ID_RE.search(url)
    return m.group(1) if m else None


_ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"


async def _transcribe_media(
    url: str, elevenlabs_api_key: str = "", stt_model: str = "scribe_v1"
) -> ContentResult | None:
    """Transcribe media. YouTube captions first, then ElevenLabs Scribe fallback."""
    video_id = _extract_youtube_id(url)
    if video_id:
        try:
            from youtube_transcript_api import YouTubeTranscriptApi

            ytt_api = YouTubeTranscriptApi()
            transcript = await asyncio.to_thread(ytt_api.fetch, video_id)
            text = " ".join(snippet.text for snippet in transcript)
            if text and len(text) >= _MIN_CONTENT_LENGTH:
                return ContentResult(
                    text=text,
                    title="",
                    word_count=len(text.split()),
                    extraction_method="youtube_captions",
                    metadata={"video_id": video_id, "segments": len(transcript)},
                )
        except Exception as exc:
            logger.warning("youtube_captions_failed", video_id=video_id, error=str(exc))

    # ElevenLabs Scribe fallback (empty key = kill switch)
    if elevenlabs_api_key:
        return await _transcribe_via_elevenlabs(url, elevenlabs_api_key, stt_model)

    return None


async def _transcribe_via_elevenlabs(
    url: str, api_key: str, stt_model: str = "scribe_v1"
) -> ContentResult | None:
    """Transcribe audio/video via ElevenLabs Scribe (https://api.elevenlabs.io)."""
    async with httpx.AsyncClient() as client:
        path = await _download_file(client, url, _MAX_MEDIA_BYTES, ".media")
        if not path:
            return None

    try:
        try:
            activity.heartbeat("elevenlabs_downloading")
        except Exception:
            pass
        async with httpx.AsyncClient(timeout=1800) as client:
            with open(path, "rb") as f:
                try:
                    activity.heartbeat("elevenlabs_transcribing")
                except Exception:
                    pass
                resp = await client.post(
                    _ELEVENLABS_STT_URL,
                    headers={"xi-api-key": api_key},
                    files={"file": ("audio", f)},
                    data={"model_id": stt_model},
                )
                resp.raise_for_status()
                data = resp.json()

        text = data.get("text", "")
        if not text or len(text) < _MIN_CONTENT_LENGTH:
            return None

        return ContentResult(
            text=text,
            title="",
            word_count=len(text.split()),
            extraction_method="elevenlabs",
            metadata={"language_code": data.get("language_code")},
        )
    except Exception as exc:
        logger.warning("elevenlabs_transcription_failed", url=url, error=str(exc))
        return None
    finally:
        if os.path.exists(path):
            os.unlink(path)


# --- Temporal activity ---


@dataclass
class ContentActivities:
    """Content extraction activities for the intelligence pipeline.

    Offloads article/PDF/image extraction to knowledge-service.
    Only media transcription (YouTube/ElevenLabs Scribe) runs locally.
    """

    knowledge_connector: Any = None
    db_pool: Any = None
    enabled: bool = True
    elevenlabs_api_key: str = ""
    elevenlabs_stt_model: str = "scribe_v1"
    raindrop_api_token: str = ""

    def _auth_headers_for(self, url: str) -> dict[str, str]:
        """Return extra auth headers needed to download from this URL."""
        if self.raindrop_api_token and "api.raindrop.io" in url:
            return {"Authorization": f"Bearer {self.raindrop_api_token}"}
        return {}

    def _needs_auth(self, url: str, content_type: str) -> bool:
        """Check if URL requires authentication and is text-fetchable locally.

        Only returns True for HTML/article content behind auth.
        PDFs and images behind auth fall through to URL-only path
        since local fetch returns binary data, not useful raw text.
        """
        if not self.raindrop_api_token or "raindrop.io" not in url:
            return False
        return content_type == "article"

    async def _fetch_authenticated(self, url: str) -> str | None:
        """Fetch content from authenticated URL. Returns raw text or None."""
        headers = self._auth_headers_for(url)
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                r = await client.get(url, headers=headers)
                r.raise_for_status()
                return r.text[:100_000]
            except Exception:
                return None

    @activity.defn
    async def ingest_content(self, item: dict) -> dict:
        """Ingest a pre-built content dict into knowledge-service.

        For callers that already have the full text and don't need URL
        scraping (CalendarIngestFlow synthesizes event text client-side via
        events_to_content). `item` carries url/title/source_type/raw_text/
        summary/tags/metadata — passed straight to the connector.
        """
        if not self.enabled or not self.knowledge_connector:
            return {"status": "disabled"}
        url = item.get("url")
        if not url:
            return {"status": "skipped", "reason": "no url"}
        return await self.knowledge_connector.ingest_content(
            url=url,
            title=item.get("title", ""),
            source_type=item.get("source_type", "content"),
            summary=item.get("summary"),
            raw_text=item.get("raw_text"),
            tags=item.get("tags"),
            metadata=item.get("metadata"),
        )

    @activity.defn
    async def process_content(
        self,
        url: str,
        title: str,
        category: str,
        fallback_text: str = "",
        extra_tags: list[str] | None = None,
    ) -> dict:
        """Fetch + extract a URL's readable text, then ingest it.

        Articles/PDFs are fetched and extracted in-process (trafilatura / pdfminer
        via `fetch_and_extract`). Media is transcribed locally. Authenticated URLs
        (e.g. Raindrop) are fetched with our headers then extracted. Images have no
        local OCR, so they fall through to the `fallback_text` path or are skipped.

        `fallback_text`: when the caller already has a summary/excerpt (RSS entry
        summary, Raindrop excerpt), it is used when extraction comes back too thin
        (paywalled / dead-link / JS-only / no-OCR-image) so the entry isn't a
        complete loss.
        """
        if not self.enabled or not self.knowledge_connector:
            return {"status": "disabled"}

        t0 = time.monotonic()
        # Rewrite arxiv /abs/ landing pages to /pdf/ before classification
        # so knowledge-service fetches the full paper, not the ~500-char
        # abstract page. See `rewrite_arxiv_abs_to_pdf` docstring.
        url = rewrite_arxiv_abs_to_pdf(url)
        content_type = detect_content_type(url)
        tags = [category] if category else []
        # Dedupe while preserving order; drop falsy extra tags.
        tags = list(dict.fromkeys([*tags, *(t for t in (extra_tags or []) if t)]))
        domains = _category_to_domains(category)
        fallback_text = (fallback_text or "").strip()

        try:
            if content_type == "media":
                result = await _transcribe_media(
                    url, self.elevenlabs_api_key, self.elevenlabs_stt_model
                )
                if not result or len(result.text) < _MIN_CONTENT_LENGTH:
                    await self._record(url, content_type, "empty", t0)
                    return {"status": "empty"}
                resp = await self.knowledge_connector.ingest_content(
                    url=url,
                    title=title or result.title or "",
                    source_type="media",
                    raw_text=result.text[:100_000],
                    tags=tags,
                    metadata={"extraction_method": result.extraction_method},
                )
            else:
                # Fetch + extract readable text in-process (the knowledge-service
                # used to do this server-side). Authenticated URLs (Raindrop) need
                # our own headers, so we fetch their HTML then extract; everything
                # else goes through the shared fetch_and_extract helper.
                extracted_title: str | None = None
                if self._needs_auth(url, content_type):
                    html = await self._fetch_authenticated(url)
                    raw_text = extract_html(html)[0] if html else ""
                else:
                    raw_text, extracted_title = await fetch_and_extract(url, content_type)

                if not raw_text or len(raw_text) < _MIN_CONTENT_LENGTH:
                    # Extraction too thin (paywall, image without OCR, dead link,
                    # JS-only SPA). Keep the caller's excerpt if there is one,
                    # else record empty and skip — a title-only row has no RAG
                    # value.
                    if fallback_text:
                        raw_text = fallback_text[:8000]
                    else:
                        await self._record(url, content_type, "empty", t0)
                        return {"status": "empty"}

                resp = await self.knowledge_connector.ingest_content(
                    url=url,
                    title=title or extracted_title or "",
                    source_type=content_type,
                    raw_text=raw_text,
                    tags=tags,
                    domains=domains,
                )

            await self._record(url, content_type, "ok", t0)
            return {
                "status": "ok",
                "job_id": resp.get("job_id"),
                "content_id": resp.get("content_id"),
            }

        except httpx.HTTPStatusError as exc:
            status = "duplicate" if exc.response.status_code == 409 else "error"
            await self._record(url, content_type, status, t0)
            return {"status": status}
        except Exception:
            await self._record(url, content_type, "error", t0)
            return {"status": "error"}

    async def _record(
        self,
        url: str,
        content_type: str,
        status: str,
        t0: float,
    ) -> None:
        """Record extraction to connector_calls. Never raises."""
        if not self.db_pool:
            return
        try:
            from aegis.observability import record_connector_call

            await record_connector_call(
                self.db_pool,
                connector="content_extraction",
                action=content_type,
                status=status,
                latency_ms=int((time.monotonic() - t0) * 1000),
                error=None if status == "ok" else status,
            )
        except Exception:
            pass
