"""Comms service configuration."""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class CommsSettings(BaseSettings):
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

    # Comms channel — Slack only. Surfaced in /api/health.
    channel: str = Field(default="slack", validation_alias=AliasChoices("AEGIS_CHANNEL"))

    # Slack credentials.
    slack_bot_token: str = Field(default="", validation_alias=AliasChoices("AEGIS_SLACK_BOT_TOKEN"))
    slack_app_token: str = Field(default="", validation_alias=AliasChoices("AEGIS_SLACK_APP_TOKEN"))

    # Curated self-signal ingest. Normally set on core's admin Integrations page
    # and pulled over /api/internal/slack-config at boot (see _merge_slack_config);
    # these env vars are the offline fallback. Blank owner id = the whole feature
    # is inert, so AEGIS can never ingest someone else's message.
    slack_owner_member_id: str = Field(
        default="", validation_alias=AliasChoices("AEGIS_SLACK_OWNER_MEMBER_ID")
    )
    slack_saveit_emoji: str = Field(
        default="brain", validation_alias=AliasChoices("AEGIS_SLACK_SAVEIT_EMOJI")
    )
    slack_note_to_self_channel: str = Field(
        default="", validation_alias=AliasChoices("AEGIS_SLACK_NOTE_TO_SELF_CHANNEL")
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

    # Voice-first capture from outside Slack (POST /api/ingest/voice — an iOS
    # Shortcut posts the recording as the raw request body). Its own secret
    # rather than AEGIS_API_KEY so a phone holds a credential that can ONLY
    # capture, not send messages as any agent. Blank = the route is off (503),
    # so an unconfigured deploy can never accept unauthenticated audio.
    voice_ingest_secret: str = Field(
        default="", validation_alias=AliasChoices("AEGIS_VOICE_INGEST_SECRET")
    )
