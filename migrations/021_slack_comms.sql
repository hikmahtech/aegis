-- Channel-abstraction comms migration: Slack channel target + neutral message ref.
ALTER TABLE agents       ADD COLUMN IF NOT EXISTS slack_channel_id TEXT;
ALTER TABLE interactions ADD COLUMN IF NOT EXISTS delivery_ref JSONB;
COMMENT ON COLUMN agents.slack_channel_id IS 'Slack channel id for this agent (active when AEGIS_CHANNEL=slack)';
COMMENT ON COLUMN interactions.delivery_ref IS 'Channel-neutral message ref {adapter, ...} for edit/delete (slack: {adapter,channel,ts}; telegram: {adapter,chat_id,message_id})';
