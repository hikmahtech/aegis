"""Phase 3 Media Pipeline integration tests.

Tests the full media pipeline: content type detection, YouTube caption extraction,
ElevenLabs Scribe HTTP client, and process_content routing. Run with docker compose infra.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from temporalio.testing import ActivityEnvironment

# --- 1. Content Type Detection (media extensions) ---


class TestMediaContentTypeDetection:
    """Verify detect_content_type routes media URLs correctly."""

    def test_youtube_standard(self):
        from aegis_worker.activities.content import detect_content_type

        assert detect_content_type("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "media"

    def test_youtube_short(self):
        from aegis_worker.activities.content import detect_content_type

        assert detect_content_type("https://youtu.be/dQw4w9WgXcQ") == "media"

    def test_mp3_podcast(self):
        from aegis_worker.activities.content import detect_content_type

        assert detect_content_type("https://podcasts.example.com/episode42.mp3") == "media"

    def test_mp4_video(self):
        from aegis_worker.activities.content import detect_content_type

        assert detect_content_type("https://cdn.example.com/talk.mp4") == "media"

    def test_m4a_audio(self):
        from aegis_worker.activities.content import detect_content_type

        assert detect_content_type("https://example.com/recording.m4a") == "media"

    def test_webm_video(self):
        from aegis_worker.activities.content import detect_content_type

        assert detect_content_type("https://example.com/stream.webm") == "media"

    def test_wav_audio(self):
        from aegis_worker.activities.content import detect_content_type

        assert detect_content_type("https://example.com/sample.wav") == "media"

    def test_article_not_media(self):
        from aegis_worker.activities.content import detect_content_type

        assert detect_content_type("https://blog.example.com/post") == "article"

    def test_pdf_not_media(self):
        from aegis_worker.activities.content import detect_content_type

        assert detect_content_type("https://example.com/paper.pdf") == "pdf"


# --- 2. YouTube Caption Extraction (end-to-end with real API) ---


class TestYouTubeCaptionExtraction:
    """Test YouTube caption extraction via youtube-transcript-api v1+."""

    async def test_extract_youtube_id_formats(self):
        """All YouTube URL formats produce correct video IDs."""
        from aegis_worker.activities.content import _extract_youtube_id

        assert _extract_youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
        assert _extract_youtube_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
        assert _extract_youtube_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
        assert (
            _extract_youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLrAXtmErr")
            == "dQw4w9WgXcQ"
        )
        assert _extract_youtube_id("https://example.com/article") is None

    async def test_transcribe_media_youtube_captions(self):
        """_transcribe_media extracts YouTube captions with v1+ API objects."""
        from aegis_worker.activities.content import _transcribe_media

        # Simulate v1+ FetchedTranscript with snippet objects
        snippets = [
            MagicMock(text=f"This is segment {i} of the video transcript. ") for i in range(20)
        ]
        mock_transcript = MagicMock()
        mock_transcript.__iter__ = MagicMock(return_value=iter(snippets))
        mock_transcript.__len__ = MagicMock(return_value=20)

        with patch("youtube_transcript_api.YouTubeTranscriptApi") as mock_cls:
            mock_api = MagicMock()
            mock_cls.return_value = mock_api
            mock_api.fetch.return_value = mock_transcript
            result = await _transcribe_media("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

        assert result is not None
        assert result.extraction_method == "youtube_captions"
        assert result.word_count > 0
        assert result.metadata["video_id"] == "dQw4w9WgXcQ"
        assert result.metadata["segments"] == 20
        mock_api.fetch.assert_called_once_with("dQw4w9WgXcQ")

    async def test_transcribe_media_no_captions_no_key(self):
        """Returns None when YouTube has no captions and Scribe is disabled (no key)."""
        from aegis_worker.activities.content import _transcribe_media

        with patch("youtube_transcript_api.YouTubeTranscriptApi") as mock_cls:
            mock_api = MagicMock()
            mock_cls.return_value = mock_api
            mock_api.fetch.side_effect = Exception("No transcript found")
            result = await _transcribe_media(
                "https://www.youtube.com/watch?v=xNoCapsHere",
                elevenlabs_api_key="",
            )

        assert result is None


# --- 3. ElevenLabs Scribe HTTP Client ---


class TestScribeHTTPClient:
    """Test transcription via the ElevenLabs Scribe API (respx-mocked)."""

    async def test_scribe_endpoint(self, tmp_path):
        """Scribe client POSTs to /v1/speech-to-text with the key + model_id."""
        import respx
        from aegis_worker.activities.content import (
            _ELEVENLABS_STT_URL,
            _transcribe_via_elevenlabs,
        )

        audio_file = tmp_path / "podcast.mp3"
        audio_file.write_bytes(b"fake audio content " * 100)

        with (
            patch("aegis_worker.activities.content._download_file", return_value=str(audio_file)),
            respx.mock,
        ):
            route = respx.post(_ELEVENLABS_STT_URL).respond(
                json={
                    "text": "Full transcription of the podcast episode with lots of content. " * 10,
                    "language_code": "en",
                }
            )
            result = await _transcribe_via_elevenlabs(
                "https://example.com/podcast.mp3",
                "el-key",
                "scribe_v1",
            )

        assert result is not None
        assert result.extraction_method == "elevenlabs"
        assert result.word_count > 0

        req = route.calls.last.request
        assert str(req.url) == _ELEVENLABS_STT_URL
        assert req.headers["xi-api-key"] == "el-key"
        # multipart body carries the audio file + the model_id field
        body = req.content
        assert b'name="file"' in body
        assert b"scribe_v1" in body

    async def test_scribe_fallback_from_youtube(self):
        """When YouTube captions fail, falls back to Scribe if a key is configured."""
        from aegis_worker.activities.content import ContentResult, _transcribe_media

        stt_result = ContentResult(
            text="Scribe transcribed this video content " * 20,
            title="",
            word_count=120,
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
            mock_api.fetch.side_effect = Exception("No transcript")
            result = await _transcribe_media(
                "https://www.youtube.com/watch?v=xNoCapsHere",
                elevenlabs_api_key="el-key",
            )

        assert result is not None
        assert result.extraction_method == "elevenlabs"
        mock_stt.assert_called_once()

    async def test_non_youtube_media_direct_to_scribe(self):
        """Non-YouTube media URLs go directly to Scribe (no YouTube caption attempt)."""
        from aegis_worker.activities.content import ContentResult, _transcribe_media

        stt_result = ContentResult(
            text="Podcast transcription with detailed content " * 20,
            title="",
            word_count=100,
            extraction_method="elevenlabs",
        )

        with patch(
            "aegis_worker.activities.content._transcribe_via_elevenlabs",
            return_value=stt_result,
        ) as mock_stt:
            result = await _transcribe_media(
                "https://example.com/podcast.mp3",
                elevenlabs_api_key="el-key",
            )

        assert result is not None
        assert result.extraction_method == "elevenlabs"
        mock_stt.assert_called_once_with(
            "https://example.com/podcast.mp3", "el-key", "scribe_v1"
        )


# --- 4. process_content Full Pipeline ---


class TestProcessContentMediaPipeline:
    """Test process_content routes media correctly through the full pipeline."""

    async def test_youtube_url_routes_to_media_pipeline(self):
        """YouTube URL -> detect_content_type -> media -> _transcribe_media -> knowledge."""
        from aegis_worker.activities.content import ContentActivities, ContentResult

        mock_knowledge = AsyncMock()
        mock_knowledge.ingest_content = AsyncMock(
            return_value={"content_id": "c-media-1", "job_id": "job-1"}
        )

        activities = ContentActivities(
            knowledge_connector=mock_knowledge,
            db_pool=AsyncMock(),
            elevenlabs_api_key="el-key",
        )
        env = ActivityEnvironment()

        media_result = ContentResult(
            text="Full video transcript with substantial content " * 50,
            title="Tech Conference Talk",
            word_count=350,
            extraction_method="youtube_captions",
        )

        with patch("aegis_worker.activities.content._transcribe_media", return_value=media_result):
            result = await env.run(
                activities.process_content,
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "Tech Conference Talk",
                "informational",
            )

        assert result["status"] == "ok"
        mock_knowledge.ingest_content.assert_called_once()

    async def test_mp3_url_routes_to_media_pipeline(self):
        """MP3 URL -> detect_content_type -> media -> _transcribe_media -> knowledge."""
        from aegis_worker.activities.content import ContentActivities, ContentResult

        mock_knowledge = AsyncMock()
        mock_knowledge.ingest_content = AsyncMock(return_value={"content_id": "c-media-2"})

        activities = ContentActivities(
            knowledge_connector=mock_knowledge,
            db_pool=AsyncMock(),
            elevenlabs_api_key="el-key",
        )
        env = ActivityEnvironment()

        stt_result = ContentResult(
            text="Podcast episode transcript about AI safety and alignment " * 40,
            title="",
            word_count=280,
            extraction_method="elevenlabs",
        )

        with patch(
            "aegis_worker.activities.content._transcribe_media", return_value=stt_result
        ):
            result = await env.run(
                activities.process_content,
                "https://podcasts.example.com/episode42.mp3",
                "AI Safety Podcast",
                "informational",
            )

        assert result["status"] == "ok"
        mock_knowledge.ingest_content.assert_called_once()

    async def test_media_failure_returns_empty(self):
        """Media transcription failure returns empty, not crash."""
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
                "https://example.com/podcast.mp3",
                "Podcast",
                "informational",
            )

        assert result["status"] == "empty"

    async def test_elevenlabs_key_passed_from_dataclass(self):
        """elevenlabs_api_key + model flow from ContentActivities to _transcribe_media."""
        from aegis_worker.activities.content import ContentActivities, ContentResult

        activities = ContentActivities(
            knowledge_connector=AsyncMock(
                ingest_content=AsyncMock(return_value={"content_id": "c-1", "job_id": "job-1"})
            ),
            db_pool=AsyncMock(),
            elevenlabs_api_key="custom-key",
            elevenlabs_stt_model="scribe_v1",
        )
        env = ActivityEnvironment()

        with patch(
            "aegis_worker.activities.content._transcribe_media",
            return_value=ContentResult(
                text="Content " * 100,
                title="",
                word_count=100,
                extraction_method="elevenlabs",
            ),
        ) as mock_transcribe:
            await env.run(
                activities.process_content,
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "Video",
                "informational",
            )

        mock_transcribe.assert_called_once_with(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "custom-key",
            "scribe_v1",
        )


# --- 5. Config + Bootstrap Wiring ---


class TestConfigWiring:
    """Test Settings and bootstrap wiring."""

    def test_settings_elevenlabs_key_default_empty(self):
        """elevenlabs_api_key defaults to empty string (disabled / kill switch)."""
        from aegis.config import Settings

        s = Settings(
            database_url="postgresql://x:x@localhost/x",
            api_key="test",
            admin_password="test",
            litellm_url="https://litellm.example.com/v1",
            temporal_ui_url="https://temporal.example.com",
            admin_username="admin",
            homelab_dagster_graphql_url="https://dagster.test/graphql",
            homelab_traefik_api_url="https://traefik.test:8080",
        )
        assert s.elevenlabs_api_key == ""
        assert s.elevenlabs_stt_model == "scribe_v1"
        assert s.tts_enabled is False

    def test_content_activities_accepts_elevenlabs_key(self):
        """ContentActivities dataclass accepts the elevenlabs_api_key field."""
        from aegis_worker.activities.content import ContentActivities

        act = ContentActivities(elevenlabs_api_key="el-key")
        assert act.elevenlabs_api_key == "el-key"

        act_default = ContentActivities()
        assert act_default.elevenlabs_api_key == ""
        assert act_default.elevenlabs_stt_model == "scribe_v1"
