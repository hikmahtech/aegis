-- Per-persona ElevenLabs voice id for outbound TTS voice notes.
-- Empty/NULL = that agent gets no voice note (text-only); preserved across a
-- core re-seed via COALESCE(NULLIF(EXCLUDED...,''), agents...) in seed.py.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS elevenlabs_voice_id TEXT;
COMMENT ON COLUMN agents.elevenlabs_voice_id IS 'ElevenLabs voice id for this agent''s TTS voice notes (active when AEGIS_TTS_ENABLED=true)';
