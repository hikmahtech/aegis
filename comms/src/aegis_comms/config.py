"""Comms service configuration."""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class TelegramSettings(BaseSettings):
    """Settings for the comms bot + delivery service (Slack)."""

    model_config = {"env_file": "config/.env", "extra": "ignore"}

    # Core API
    core_url: str = Field(
        default="http://localhost:8080", validation_alias=AliasChoices("AEGIS_CORE_URL")
    )
    api_key: str = Field(default="", validation_alias=AliasChoices("AEGIS_API_KEY"))
    admin_username: str = Field(
        default="admin", validation_alias=AliasChoices("AEGIS_ADMIN_USERNAME")
    )
    admin_password: str = Field(
        default="admin", validation_alias=AliasChoices("AEGIS_ADMIN_PASSWORD")
    )

    # Delivery server
    host: str = "0.0.0.0"
    port: int = 8081

    # Comms channel — Slack only (Telegram retired). Surfaced in /api/health.
    channel: str = Field(default="slack", validation_alias=AliasChoices("AEGIS_CHANNEL"))

    # Slack credentials.
    slack_bot_token: str = Field(default="", validation_alias=AliasChoices("AEGIS_SLACK_BOT_TOKEN"))
    slack_app_token: str = Field(default="", validation_alias=AliasChoices("AEGIS_SLACK_APP_TOKEN"))
    slack_signing_secret: str = Field(
        default="", validation_alias=AliasChoices("AEGIS_SLACK_SIGNING_SECRET")
    )

    # ElevenLabs (hosted vendor — NOT the LiteLLM proxy). Drives inbound voice-note
    # transcription (Scribe STT) and outbound per-persona voice notes (TTS).
    # Empty key = both disabled.
    elevenlabs_api_key: str = Field(
        default="", validation_alias=AliasChoices("AEGIS_ELEVENLABS_API_KEY")
    )
    elevenlabs_stt_model: str = Field(
        default="scribe_v1", validation_alias=AliasChoices("AEGIS_ELEVENLABS_STT_MODEL")
    )
    elevenlabs_tts_model: str = Field(
        default="eleven_multilingual_v2",
        validation_alias=AliasChoices("AEGIS_ELEVENLABS_TTS_MODEL"),
    )
