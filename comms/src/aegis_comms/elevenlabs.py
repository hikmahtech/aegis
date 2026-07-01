"""Tiny ElevenLabs client for comms (STT inbound + TTS outbound).

comms has no aegis-core dependency, so it talks to the ElevenLabs vendor API
directly (the same way it does its own PDF extraction locally). ElevenLabs is a
separate vendor — NOT the LiteLLM proxy, which serves text LLMs only.
"""

from __future__ import annotations

import httpx
import structlog

logger = structlog.get_logger()

_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"
_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"


async def transcribe(
    audio: bytes,
    *,
    api_key: str,
    model_id: str = "scribe_v1",
    filename: str = "audio",
) -> str | None:
    """Transcribe audio bytes via ElevenLabs Scribe. Returns text or None."""
    if not api_key or not audio:
        return None
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                _STT_URL,
                headers={"xi-api-key": api_key},
                files={"file": (filename, audio)},
                data={"model_id": model_id},
            )
            resp.raise_for_status()
            return resp.json().get("text") or None
    except Exception as exc:  # noqa: BLE001 — best-effort; caller degrades
        logger.warning("elevenlabs_transcribe_failed", error=str(exc)[:200])
        return None


async def synthesize(
    text: str,
    *,
    api_key: str,
    voice_id: str,
    model_id: str = "eleven_multilingual_v2",
) -> bytes | None:
    """Synthesize speech (mp3 bytes) via ElevenLabs TTS. Returns None if disabled."""
    if not api_key or not voice_id or not text.strip():
        return None
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                _TTS_URL.format(voice_id=voice_id),
                headers={"xi-api-key": api_key, "accept": "audio/mpeg"},
                json={"text": text, "model_id": model_id},
            )
            resp.raise_for_status()
            return resp.content or None
    except Exception as exc:  # noqa: BLE001 — best-effort; caller degrades
        logger.warning("elevenlabs_synthesize_failed", error=str(exc)[:200])
        return None
