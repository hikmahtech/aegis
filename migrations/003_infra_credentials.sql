-- Encrypted per-host credentials, BYO-keys style (same {value, encrypted}
-- envelope as the settings 'slack' row — see aegis.crypto):
--   {"ssh_private_key_enc": {...}, "kubeconfig_enc": {...}}
-- Never exposed through the admin API; only has_ssh_key/has_kubeconfig booleans.
ALTER TABLE infra ADD COLUMN credentials jsonb NOT NULL DEFAULT '{}'::jsonb;
