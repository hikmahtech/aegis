-- Metrics tracking for published social posts (see docs/social-publishing.md
-- "Metrics" section). Postiz analytics are pulled per posted_ref and cached
-- on the outbox row — idempotent, additive-only.

ALTER TABLE social_outbox
    ADD COLUMN IF NOT EXISTS metrics jsonb DEFAULT '{}'::jsonb NOT NULL,
    ADD COLUMN IF NOT EXISTS metrics_at timestamptz;
