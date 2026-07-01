"""Tests for content activities (in-process fetch + extract + ingest).

ContentActivities fetches and extracts a URL's text locally (trafilatura/pdfminer
via fetch_and_extract), transcribes media locally, and ingests into the knowledge
subsystem. These tests patch the extraction helpers to unit-test the branching.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from aegis_worker.activities.content import (
    ContentActivities,
    _category_to_domains,
    detect_content_type,
)


class TestDetectContentType:
    """Test URL-based content type detection."""

    def test_article(self):
        assert detect_content_type("https://example.com/page") == "article"

    def test_pdf(self):
        assert detect_content_type("https://example.com/doc.pdf") == "pdf"

    def test_image(self):
        assert detect_content_type("https://example.com/photo.jpg") == "image"

    def test_media_youtube(self):
        assert detect_content_type("https://www.youtube.com/watch?v=abc") == "media"

    def test_media_mp3(self):
        assert detect_content_type("https://example.com/audio.mp3") == "media"


class TestCategoryToDomains:
    """Test triage category to knowledge-service domain mapping."""

    def test_technology(self):
        assert _category_to_domains("technology") == ["technology", "infrastructure"]

    def test_finance(self):
        assert _category_to_domains("finance") == ["finance"]

    def test_research(self):
        assert _category_to_domains("research") == ["science"]

    def test_news(self):
        assert _category_to_domains("news") == ["news"]

    def test_unknown_category(self):
        assert _category_to_domains("random_category") == []

    def test_empty_category(self):
        assert _category_to_domains("") == []


def _make_activities(
    knowledge_connector=None,
    enabled=True,
    elevenlabs_api_key="",
    raindrop_api_token="",
):
    """Helper to create ContentActivities with a mocked db_pool."""
    mock_pool = MagicMock()
    mock_conn = AsyncMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return ContentActivities(
        knowledge_connector=knowledge_connector,
        db_pool=mock_pool,
        enabled=enabled,
        elevenlabs_api_key=elevenlabs_api_key,
        raindrop_api_token=raindrop_api_token,
    )


def _patch_extract(text, title=None):
    """Patch the module-level fetch_and_extract to return controlled output."""
    return patch(
        "aegis_worker.activities.content.fetch_and_extract",
        AsyncMock(return_value=(text, title)),
    )


class TestProcessContentExtraction:
    """Articles/PDFs are fetched + extracted locally, then ingested with raw_text."""

    async def test_article_extracts_and_ingests(self):
        mock_kc = AsyncMock()
        mock_kc.ingest_content.return_value = {
            "content_id": "abc", "job_id": "job-1", "status": "accepted",
        }
        act = _make_activities(knowledge_connector=mock_kc)

        with _patch_extract("Extracted article body. " * 20, "Extracted Title"):
            result = await act.process_content(
                "https://example.com/article", "Test Article", "technology"
            )

        assert result["status"] == "ok"
        assert result["job_id"] == "job-1"
        call_kwargs = mock_kc.ingest_content.call_args.kwargs
        assert call_kwargs["raw_text"].startswith("Extracted article body")
        assert call_kwargs["domains"] == ["technology", "infrastructure"]
        assert call_kwargs["tags"] == ["technology"]

    async def test_pdf_extracts_and_ingests(self):
        mock_kc = AsyncMock()
        mock_kc.ingest_content.return_value = {
            "content_id": "abc", "job_id": "job-2", "status": "accepted",
        }
        act = _make_activities(knowledge_connector=mock_kc)

        with _patch_extract("Extracted PDF text. " * 20):
            result = await act.process_content("https://example.com/doc.pdf", "Test PDF", "research")

        assert result["status"] == "ok"
        call_kwargs = mock_kc.ingest_content.call_args.kwargs
        assert call_kwargs["raw_text"].startswith("Extracted PDF text")
        assert call_kwargs["domains"] == ["science"]

    async def test_image_without_ocr_is_skipped(self):
        """Image URLs yield no local text (no OCR); without a fallback they're skipped."""
        mock_kc = AsyncMock()
        act = _make_activities(knowledge_connector=mock_kc)

        with _patch_extract("", None):
            result = await act.process_content("https://example.com/photo.png", "Photo", "news")

        assert result["status"] == "empty"
        mock_kc.ingest_content.assert_not_called()

    async def test_thin_extract_falls_back_to_excerpt(self):
        """When extraction is too thin, the caller's excerpt is used instead of skipping."""
        mock_kc = AsyncMock()
        mock_kc.ingest_content.return_value = {"content_id": "abc", "job_id": "job-f", "status": "ok"}
        act = _make_activities(knowledge_connector=mock_kc)

        with _patch_extract("", None):
            result = await act.process_content(
                "https://example.com/paywalled", "Paywalled", "news",
                fallback_text="The caller's excerpt with enough words to matter.",
            )

        assert result["status"] == "ok"
        call_kwargs = mock_kc.ingest_content.call_args.kwargs
        assert call_kwargs["raw_text"].startswith("The caller's excerpt")

    async def test_empty_category_gives_empty_tags_and_domains(self):
        mock_kc = AsyncMock()
        mock_kc.ingest_content.return_value = {
            "content_id": "abc", "job_id": "job-4", "status": "accepted",
        }
        act = _make_activities(knowledge_connector=mock_kc)

        with _patch_extract("Some body text. " * 20):
            result = await act.process_content("https://example.com/page", "Page", "")

        assert result["status"] == "ok"
        call_kwargs = mock_kc.ingest_content.call_args.kwargs
        assert call_kwargs["tags"] == []
        assert call_kwargs["domains"] == []


class TestProcessContentMedia:
    """Test that media transcription still happens locally."""

    async def test_media_transcribes_and_sends_raw_text(self):
        """Media content is transcribed locally, raw_text sent to knowledge-service."""
        from aegis_worker.activities.content import ContentResult

        mock_kc = AsyncMock()
        mock_kc.ingest_content.return_value = {
            "content_id": "abc",
            "job_id": "job-5",
            "status": "accepted",
        }
        act = _make_activities(knowledge_connector=mock_kc, elevenlabs_api_key="el-key")

        media_result = ContentResult(
            text="Transcript content " * 50,
            title="Video Title",
            word_count=100,
            extraction_method="youtube_captions",
        )

        with patch(
            "aegis_worker.activities.content._transcribe_media",
            return_value=media_result,
        ):
            result = await act.process_content(
                "https://www.youtube.com/watch?v=abc123",
                "Video Title",
                "technology",
            )

        assert result["status"] == "ok"
        assert result["job_id"] == "job-5"
        call_kwargs = mock_kc.ingest_content.call_args.kwargs
        assert call_kwargs["raw_text"] is not None
        assert call_kwargs["source_type"] == "media"
        assert "extraction_method" in call_kwargs["metadata"]

    async def test_media_empty_transcript_returns_empty(self):
        """Empty media transcription returns empty status."""
        mock_kc = AsyncMock()
        act = _make_activities(knowledge_connector=mock_kc)

        with patch(
            "aegis_worker.activities.content._transcribe_media",
            return_value=None,
        ):
            result = await act.process_content(
                "https://www.youtube.com/watch?v=abc123",
                "Video",
                "technology",
            )

        assert result["status"] == "empty"
        mock_kc.ingest_content.assert_not_called()


class TestProcessContentDisabled:
    """Test disabled/error states."""

    async def test_disabled(self):
        act = _make_activities(enabled=False)
        result = await act.process_content("https://example.com", "Test", "tech")
        assert result["status"] == "disabled"

    async def test_no_knowledge_connector(self):
        act = _make_activities(knowledge_connector=None)
        result = await act.process_content("https://example.com", "Test", "tech")
        assert result["status"] == "disabled"

    async def test_duplicate_409(self):
        """HTTP 409 returns duplicate status."""
        mock_kc = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 409
        mock_kc.ingest_content.side_effect = httpx.HTTPStatusError(
            "409", request=MagicMock(), response=mock_response
        )
        act = _make_activities(knowledge_connector=mock_kc)

        with _patch_extract("Extracted body. " * 20):
            result = await act.process_content("https://example.com/article", "Test", "technology")

        assert result["status"] == "duplicate"

    async def test_generic_error(self):
        """Generic exceptions return error status."""
        mock_kc = AsyncMock()
        mock_kc.ingest_content.side_effect = RuntimeError("boom")
        act = _make_activities(knowledge_connector=mock_kc)

        with _patch_extract("Extracted body. " * 20):
            result = await act.process_content("https://example.com/article", "Test", "technology")

        assert result["status"] == "error"


class TestNeedsAuth:
    """Test auth detection for authenticated URLs."""

    def test_raindrop_article_with_token(self):
        act = _make_activities(raindrop_api_token="test-token")
        assert act._needs_auth("https://api.raindrop.io/rest/v1/raindrop/12345", "article") is True

    def test_raindrop_pdf_with_token(self):
        """PDFs behind auth fall through to URL-only path (no local fetch for binary)."""
        act = _make_activities(raindrop_api_token="test-token")
        assert act._needs_auth("https://api.raindrop.io/rest/v1/raindrop/12345", "pdf") is False

    def test_non_raindrop(self):
        act = _make_activities(raindrop_api_token="test-token")
        assert act._needs_auth("https://example.com/article", "article") is False

    def test_raindrop_without_token(self):
        act = _make_activities(raindrop_api_token="")
        assert act._needs_auth("https://api.raindrop.io/rest/v1/raindrop/12345", "article") is False


class TestAuthenticatedFetch:
    """Test authenticated URL fetching."""

    async def test_fetches_with_auth_and_sends_raw_text(self):
        """Authenticated URLs are fetched locally, raw_text sent to knowledge-service."""
        mock_kc = AsyncMock()
        mock_kc.ingest_content.return_value = {
            "content_id": "abc",
            "job_id": "job-6",
            "status": "accepted",
        }
        act = _make_activities(
            knowledge_connector=mock_kc,
            raindrop_api_token="test-token",
        )

        fake_html = "<html><body>" + "Content " * 100 + "</body></html>"

        with (
            patch("aegis_worker.activities.content.httpx.AsyncClient") as mock_client_cls,
            patch(
                "aegis_worker.activities.content.extract_html",
                return_value=("Extracted auth article text. " * 10, None),
            ),
        ):
            mock_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.text = fake_html
            mock_resp.raise_for_status = MagicMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await act.process_content(
                "https://api.raindrop.io/rest/v1/raindrop/12345",
                "Raindrop Item",
                "technology",
            )

        assert result["status"] == "ok"
        call_kwargs = mock_kc.ingest_content.call_args.kwargs
        assert call_kwargs["raw_text"].startswith("Extracted auth article text")

    async def test_auth_fetch_failure_returns_empty(self):
        """Failed authenticated fetch returns empty status."""
        mock_kc = AsyncMock()
        act = _make_activities(
            knowledge_connector=mock_kc,
            raindrop_api_token="test-token",
        )

        with patch("aegis_worker.activities.content.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("failed"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await act.process_content(
                "https://api.raindrop.io/rest/v1/raindrop/12345",
                "Raindrop Item",
                "technology",
            )

        assert result["status"] == "empty"
        mock_kc.ingest_content.assert_not_called()
