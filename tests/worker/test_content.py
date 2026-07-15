"""Tests for content extraction activities.

Note: Tests for deleted extraction functions (_fetch_tier1, _fetch_tier2,
_extract_pdf_text, _ocr_pdf, _ocr_image) have been removed as part of the
knowledge-first integration. Those functions are now handled by knowledge-service.
See test_content_simplified.py for the new offload-based tests.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from temporalio.testing import ActivityEnvironment


class TestDetectContentType:
    """Test URL-based content type detection."""

    def test_pdf_url(self):
        from aegis_worker.activities.content import detect_content_type

        assert detect_content_type("https://example.com/paper.pdf") == "pdf"

    def test_image_urls(self):
        from aegis_worker.activities.content import detect_content_type

        assert detect_content_type("https://example.com/photo.png") == "image"
        assert detect_content_type("https://example.com/photo.jpg") == "image"
        assert detect_content_type("https://example.com/photo.jpeg") == "image"
        assert detect_content_type("https://example.com/photo.webp") == "image"

    def test_youtube_urls(self):
        from aegis_worker.activities.content import detect_content_type

        assert detect_content_type("https://www.youtube.com/watch?v=abc123") == "media"
        assert detect_content_type("https://youtu.be/abc123") == "media"

    def test_arxiv_rewrite(self):
        from aegis_worker.activities.content import detect_content_type

        assert detect_content_type("https://arxiv.org/abs/2301.00001") == "pdf"


class TestRewriteArxivAbsToPdf:
    """``/abs/`` is a 500-char HTML wrapper around the abstract. ``/pdf/<id>.pdf``
    is the full paper (~5 MB on a typical CS paper, 117x more content). Sending
    /abs/ to knowledge-service produces 0-yield extraction; rewrite to /pdf/
    so PyMuPDF can extract the actual paper text. Triage: 154 of 200 jobs
    on 2026-05-25 were arxiv items with zero triples emitted."""

    def test_modern_id_rewritten(self):
        from aegis_worker.activities.content import rewrite_arxiv_abs_to_pdf

        assert (
            rewrite_arxiv_abs_to_pdf("https://arxiv.org/abs/2605.22738")
            == "https://arxiv.org/pdf/2605.22738.pdf"
        )

    def test_versioned_id_rewritten(self):
        from aegis_worker.activities.content import rewrite_arxiv_abs_to_pdf

        assert (
            rewrite_arxiv_abs_to_pdf("https://arxiv.org/abs/2605.22738v2")
            == "https://arxiv.org/pdf/2605.22738v2.pdf"
        )

    def test_old_style_subject_prefix_id_rewritten(self):
        """Pre-2007 arxiv IDs are ``<subject>/<digits>`` e.g.
        ``math/0211159``; the slash must survive into ``/pdf/``."""
        from aegis_worker.activities.content import rewrite_arxiv_abs_to_pdf

        assert (
            rewrite_arxiv_abs_to_pdf("https://arxiv.org/abs/math/0211159")
            == "https://arxiv.org/pdf/math/0211159.pdf"
        )

    def test_already_pdf_url_passes_through(self):
        from aegis_worker.activities.content import rewrite_arxiv_abs_to_pdf

        url = "https://arxiv.org/pdf/2605.22738.pdf"
        assert rewrite_arxiv_abs_to_pdf(url) == url

    def test_non_arxiv_url_passes_through(self):
        from aegis_worker.activities.content import rewrite_arxiv_abs_to_pdf

        url = "https://example.com/some/article"
        assert rewrite_arxiv_abs_to_pdf(url) == url

    def test_rewritten_url_still_classifies_as_pdf(self):
        """The classifier relies on the ``.pdf`` extension — the rewrite
        appends ``.pdf`` for exactly this reason."""
        from aegis_worker.activities.content import (
            detect_content_type,
            rewrite_arxiv_abs_to_pdf,
        )

        rewritten = rewrite_arxiv_abs_to_pdf("https://arxiv.org/abs/2605.22738")
        assert detect_content_type(rewritten) == "pdf"

    def test_default_is_article(self):
        from aegis_worker.activities.content import detect_content_type

        assert detect_content_type("https://simonwillison.net/2026/Mar/1/llms/") == "article"
        assert detect_content_type("https://example.com/page") == "article"

    def test_url_with_query_params(self):
        from aegis_worker.activities.content import detect_content_type

        assert detect_content_type("https://example.com/doc.pdf?token=abc") == "pdf"

    def test_gif_is_image(self):
        from aegis_worker.activities.content import detect_content_type

        assert detect_content_type("https://example.com/anim.gif") == "image"

    def test_mobile_youtube(self):
        from aegis_worker.activities.content import detect_content_type

        assert detect_content_type("https://m.youtube.com/watch?v=xyz") == "media"

    def test_audio_extensions(self):
        from aegis_worker.activities.content import detect_content_type

        assert detect_content_type("https://example.com/podcast.mp3") == "media"
        assert detect_content_type("https://example.com/episode.m4a") == "media"
        assert detect_content_type("https://example.com/audio.wav") == "media"
        assert detect_content_type("https://example.com/track.flac") == "media"
        assert detect_content_type("https://example.com/song.ogg") == "media"
        assert detect_content_type("https://example.com/clip.aac") == "media"

    def test_video_extensions(self):
        from aegis_worker.activities.content import detect_content_type

        assert detect_content_type("https://example.com/video.mp4") == "media"
        assert detect_content_type("https://example.com/stream.webm") == "media"

    def test_media_extension_with_query_params(self):
        from aegis_worker.activities.content import detect_content_type

        assert detect_content_type("https://cdn.example.com/ep42.mp3?token=abc") == "media"


class TestProcessContent:
    """Test the process_content Temporal activity (knowledge-first version)."""

    async def test_article_extracts_and_ingests(self):
        """process_content fetches+extracts the article text locally, then ingests."""
        from unittest.mock import patch

        from aegis_worker.activities.content import ContentActivities

        mock_knowledge = AsyncMock()
        mock_knowledge.ingest_content = AsyncMock(
            return_value={"content_id": "c-123", "job_id": "job-1"}
        )
        mock_pool = AsyncMock()

        activities = ContentActivities(
            knowledge_connector=mock_knowledge,
            db_pool=mock_pool,
        )

        env = ActivityEnvironment()

        with patch(
            "aegis_worker.activities.content.fetch_and_extract",
            AsyncMock(return_value=("Extracted article body. " * 20, None)),
        ):
            result = await env.run(
                activities.process_content,
                "https://example.com/article",
                "Test Article",
                "technology",
            )

        assert result["status"] == "ok"
        assert result["job_id"] == "job-1"
        mock_knowledge.ingest_content.assert_called_once()
        assert mock_knowledge.ingest_content.call_args.kwargs["raw_text"].startswith(
            "Extracted article body"
        )

    async def test_handles_duplicate(self):
        """process_content silently handles duplicate URL (409)."""
        from unittest.mock import patch

        from aegis_worker.activities.content import ContentActivities

        mock_response = MagicMock()
        mock_response.status_code = 409
        mock_knowledge = AsyncMock()
        mock_knowledge.ingest_content = AsyncMock(
            side_effect=httpx.HTTPStatusError("409", request=MagicMock(), response=mock_response)
        )
        mock_pool = AsyncMock()

        activities = ContentActivities(
            knowledge_connector=mock_knowledge,
            db_pool=mock_pool,
        )

        env = ActivityEnvironment()

        with patch(
            "aegis_worker.activities.content.fetch_and_extract",
            AsyncMock(return_value=("Extracted body. " * 20, None)),
        ):
            result = await env.run(
                activities.process_content,
                "https://example.com/dupe",
                "Dupe Article",
                "informational",
            )

        assert result["status"] == "duplicate"

    async def test_disabled_returns_early(self):
        """process_content returns disabled when feature is off."""
        from aegis_worker.activities.content import ContentActivities

        activities = ContentActivities(enabled=False)
        env = ActivityEnvironment()

        result = await env.run(
            activities.process_content,
            "https://example.com/article",
            "Test",
            "informational",
        )

        assert result["status"] == "disabled"

    async def test_media_transcription_failure_returns_empty(self):
        """process_content returns empty when media transcription returns None."""
        from aegis_worker.activities.content import ContentActivities

        mock_knowledge = AsyncMock()
        mock_pool = AsyncMock()

        activities = ContentActivities(
            knowledge_connector=mock_knowledge,
            db_pool=mock_pool,
        )

        env = ActivityEnvironment()

        with patch("aegis_worker.activities.content._transcribe_media", return_value=None):
            result = await env.run(
                activities.process_content,
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "Video",
                "informational",
            )

        assert result["status"] == "empty"

    async def test_routes_youtube_to_media_pipeline(self):
        """process_content routes YouTube URLs to media transcription."""
        from aegis_worker.activities.content import ContentActivities, ContentResult

        mock_knowledge = AsyncMock()
        mock_knowledge.ingest_content = AsyncMock(
            return_value={"content_id": "c-1", "job_id": "job-1"}
        )
        activities = ContentActivities(
            knowledge_connector=mock_knowledge,
            db_pool=AsyncMock(),
            elevenlabs_api_key="el-key",
        )
        env = ActivityEnvironment()

        media_result = ContentResult(
            text="Video transcript content " * 50,
            title="Tech Talk",
            word_count=200,
            extraction_method="youtube_captions",
        )

        with patch(
            "aegis_worker.activities.content._transcribe_media", return_value=media_result
        ) as mock_transcribe:
            result = await env.run(
                activities.process_content,
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "Tech Talk",
                "informational",
            )

        assert result["status"] == "ok"
        mock_transcribe.assert_called_once_with(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "el-key", "scribe_v1"
        )
        mock_knowledge.ingest_content.assert_called_once()

    async def test_media_no_result_returns_empty(self):
        """process_content returns empty when media transcription fails."""
        from aegis_worker.activities.content import ContentActivities

        activities = ContentActivities(
            knowledge_connector=AsyncMock(),
            db_pool=AsyncMock(),
            elevenlabs_api_key="",
        )
        env = ActivityEnvironment()

        with patch("aegis_worker.activities.content._transcribe_media", return_value=None):
            result = await env.run(
                activities.process_content,
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "Video",
                "informational",
            )

        assert result["status"] == "empty"

    async def test_no_connector_returns_disabled(self):
        """process_content returns disabled when knowledge_connector is None."""
        from aegis_worker.activities.content import ContentActivities

        activities = ContentActivities(
            knowledge_connector=None,
            db_pool=AsyncMock(),
        )
        env = ActivityEnvironment()

        result = await env.run(
            activities.process_content,
            "https://example.com/article",
            "Test",
            "informational",
        )

        assert result["status"] == "disabled"


class TestDownloadFile:
    """Test file download helper."""

    async def test_download_success(self):
        """Downloads file to temp path within size limit."""
        from aegis_worker.activities.content import _download_file

        chunks = [b"hello " * 100]

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.aiter_bytes = lambda chunk_size: _async_iter(chunks)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=mock_response)

        path = await _download_file(mock_client, "https://example.com/file.pdf", 10000, ".pdf")
        assert path is not None
        assert path.endswith(".pdf")
        assert os.path.exists(path)
        with open(path, "rb") as f:
            content = f.read()
        assert content == b"hello " * 100
        os.unlink(path)

    async def test_download_exceeds_max_bytes(self):
        """Returns None when file exceeds max_bytes."""
        from aegis_worker.activities.content import _download_file

        chunks = [b"x" * 500, b"x" * 500, b"x" * 500]

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.aiter_bytes = lambda chunk_size: _async_iter(chunks)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=mock_response)

        path = await _download_file(mock_client, "https://example.com/big.pdf", 1000, ".pdf")
        assert path is None

    async def test_download_returns_none_on_error(self):
        """Returns None on HTTP error."""
        from aegis_worker.activities.content import _download_file

        mock_client = AsyncMock()
        mock_client.stream = MagicMock(side_effect=httpx.ConnectError("connection failed"))

        path = await _download_file(mock_client, "https://example.com/fail.pdf", 10000, ".pdf")
        assert path is None


class TestExtractYoutubeId:
    """Test YouTube video ID extraction."""

    def test_standard_url(self):
        from aegis.services.content_extract import extract_youtube_id as _extract_youtube_id

        assert _extract_youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_short_url(self):
        from aegis.services.content_extract import extract_youtube_id as _extract_youtube_id

        assert _extract_youtube_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_embed_url(self):
        from aegis.services.content_extract import extract_youtube_id as _extract_youtube_id

        assert _extract_youtube_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_non_youtube_returns_none(self):
        from aegis.services.content_extract import extract_youtube_id as _extract_youtube_id

        assert _extract_youtube_id("https://example.com/article") is None


class TestTranscribeMedia:
    """Test media transcription."""

    async def test_youtube_caption_extraction(self):
        """transcribe_media fetches YouTube captions via the v1+ API."""
        from aegis_worker.activities.content import _transcribe_media

        # youtube-transcript-api v1+ returns FetchedTranscript with snippet objects
        mock_snippets = [
            MagicMock(text=f"Transcript segment {i} with substantial content for testing. ")
            for i in range(20)
        ]

        mock_transcript = MagicMock()
        mock_transcript.__iter__ = MagicMock(return_value=iter(mock_snippets))
        mock_transcript.__len__ = MagicMock(return_value=20)

        with patch("youtube_transcript_api.YouTubeTranscriptApi") as mock_cls:
            mock_api = MagicMock()
            mock_cls.return_value = mock_api
            mock_api.fetch.return_value = mock_transcript
            result = await _transcribe_media("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

        assert result is not None
        assert "Transcript segment" in result.text
        assert result.extraction_method == "youtube_captions"
        mock_api.fetch.assert_called_once_with("dQw4w9WgXcQ")

    async def test_youtube_no_captions_no_key_returns_none(self):
        """transcribe_media returns None when YouTube has no captions and no key."""
        from aegis_worker.activities.content import _transcribe_media

        with patch("youtube_transcript_api.YouTubeTranscriptApi") as mock_cls:
            mock_api = MagicMock()
            mock_cls.return_value = mock_api
            mock_api.fetch.side_effect = Exception("No captions")
            result = await _transcribe_media(
                "https://www.youtube.com/watch?v=xNoCapsHere",
                elevenlabs_api_key="",
            )

        assert result is None

    async def test_non_youtube_no_key_returns_none(self):
        """transcribe_media returns None for non-YouTube URLs without a key."""
        from aegis_worker.activities.content import _transcribe_media

        result = await _transcribe_media("https://example.com/podcast.mp3", elevenlabs_api_key="")
        assert result is None

    async def test_youtube_captions_too_short_falls_through(self):
        """transcribe_media falls through to Scribe when captions are below min length."""
        from aegis_worker.activities.content import _transcribe_media

        mock_snippet = MagicMock()
        mock_snippet.text = "Hi"

        mock_transcript = MagicMock()
        mock_transcript.__iter__ = MagicMock(return_value=iter([mock_snippet]))
        mock_transcript.__len__ = MagicMock(return_value=1)

        with (
            patch("youtube_transcript_api.YouTubeTranscriptApi") as mock_cls,
            patch(
                "aegis_worker.activities.content._transcribe_via_elevenlabs", return_value=None
            ) as mock_stt,
        ):
            mock_api = MagicMock()
            mock_cls.return_value = mock_api
            mock_api.fetch.return_value = mock_transcript
            result = await _transcribe_media(
                "https://www.youtube.com/watch?v=xShortVidId",
                elevenlabs_api_key="el-key",
            )

        assert result is None
        mock_stt.assert_called_once()

    async def test_scribe_fallback_on_no_captions(self):
        """transcribe_media falls back to Scribe when YouTube captions fail."""
        from aegis_worker.activities.content import ContentResult, _transcribe_media

        stt_result = ContentResult(
            text="Scribe transcribed content " * 20,
            title="",
            word_count=60,
            extraction_method="elevenlabs",
        )

        with (
            patch("youtube_transcript_api.YouTubeTranscriptApi") as mock_cls,
            patch(
                "aegis_worker.activities.content._transcribe_via_elevenlabs",
                return_value=stt_result,
            ) as mock_stt,
        ):
            mock_api = MagicMock()
            mock_cls.return_value = mock_api
            mock_api.fetch.side_effect = Exception("No captions")
            result = await _transcribe_media(
                "https://www.youtube.com/watch?v=xNoCapsHere",
                elevenlabs_api_key="el-key",
            )

        assert result is not None
        assert result.extraction_method == "elevenlabs"
        mock_stt.assert_called_once_with(
            "https://www.youtube.com/watch?v=xNoCapsHere", "el-key", "scribe_v1"
        )

    async def test_non_youtube_uses_scribe_directly(self):
        """transcribe_media uses Scribe directly for non-YouTube media URLs."""
        from aegis_worker.activities.content import ContentResult, _transcribe_media

        stt_result = ContentResult(
            text="Podcast transcript content " * 20,
            title="",
            word_count=60,
            extraction_method="elevenlabs",
        )

        with patch(
            "aegis_worker.activities.content._transcribe_via_elevenlabs", return_value=stt_result
        ) as mock_stt:
            result = await _transcribe_media(
                "https://example.com/podcast.mp3",
                elevenlabs_api_key="el-key",
            )

        assert result is not None
        assert result.extraction_method == "elevenlabs"
        mock_stt.assert_called_once()


class TestTranscribeViaElevenLabs:
    """Test the ElevenLabs Scribe HTTP transcription client (respx-mocked)."""

    async def test_scribe_success(self, tmp_path):
        """_transcribe_via_elevenlabs posts audio to the Scribe endpoint with the key."""
        import respx
        from aegis_worker.activities.content import (
            _ELEVENLABS_STT_URL,
            _transcribe_via_elevenlabs,
        )

        audio_file = tmp_path / "audio.mp3"
        audio_file.write_bytes(b"fake audio data")

        with (
            patch("aegis_worker.activities.content._download_file", return_value=str(audio_file)),
            respx.mock,
        ):
            route = respx.post(_ELEVENLABS_STT_URL).respond(
                json={"text": "Transcribed audio content " * 20, "language_code": "en"}
            )
            result = await _transcribe_via_elevenlabs("https://example.com/audio.mp3", "el-key")

        assert result is not None
        assert result.extraction_method == "elevenlabs"
        assert "Transcribed audio content" in result.text
        assert result.metadata["language_code"] == "en"
        assert route.called
        assert route.calls.last.request.headers["xi-api-key"] == "el-key"

    async def test_scribe_download_failure_returns_none(self):
        """_transcribe_via_elevenlabs returns None when download fails."""
        from aegis_worker.activities.content import _transcribe_via_elevenlabs

        with patch("aegis_worker.activities.content._download_file", return_value=None):
            result = await _transcribe_via_elevenlabs("https://example.com/big.mp4", "el-key")

        assert result is None

    async def test_scribe_short_text_returns_none(self, tmp_path):
        """_transcribe_via_elevenlabs returns None when transcription is too short."""
        import respx
        from aegis_worker.activities.content import (
            _ELEVENLABS_STT_URL,
            _transcribe_via_elevenlabs,
        )

        audio_file = tmp_path / "audio.mp3"
        audio_file.write_bytes(b"fake audio data")

        with (
            patch("aegis_worker.activities.content._download_file", return_value=str(audio_file)),
            respx.mock,
        ):
            respx.post(_ELEVENLABS_STT_URL).respond(json={"text": "Hi"})
            result = await _transcribe_via_elevenlabs("https://example.com/short.mp3", "el-key")

        assert result is None

    async def test_scribe_http_error_returns_none(self, tmp_path):
        """_transcribe_via_elevenlabs returns None on HTTP error from Scribe."""
        import respx
        from aegis_worker.activities.content import (
            _ELEVENLABS_STT_URL,
            _transcribe_via_elevenlabs,
        )

        audio_file = tmp_path / "audio.mp3"
        audio_file.write_bytes(b"fake audio data")

        with (
            patch("aegis_worker.activities.content._download_file", return_value=str(audio_file)),
            respx.mock,
        ):
            respx.post(_ELEVENLABS_STT_URL).respond(status_code=500)
            result = await _transcribe_via_elevenlabs("https://example.com/audio.mp3", "el-key")

        assert result is None


async def _async_iter(items):
    """Helper to create an async iterator from a list."""
    for item in items:
        yield item
