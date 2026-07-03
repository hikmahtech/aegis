-- Social publishing (MVP: X/Twitter). A Todoist task (@publish + platform
-- labels) becomes a post after an approval card — see docs/social-publishing.md.
-- Tokens live per-account here as Fernet stored-secret dicts via aegis.crypto
-- ({value, encrypted} jsonb), NOT in env vars or token files: lifetimes and
-- refresh semantics differ per platform.

CREATE TABLE IF NOT EXISTS social_accounts (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    platform          text NOT NULL,   -- 'x' | 'linkedin' | 'facebook' | 'youtube'
    label             text NOT NULL,   -- e.g. 'hikmah' | 'personal' — multiple accounts per platform
    access_token_enc  jsonb,           -- {value, encrypted} via aegis.crypto
    refresh_token_enc jsonb,
    expires_at        timestamptz,
    meta              jsonb DEFAULT '{}'::jsonb NOT NULL,  -- scopes, page_id, org_urn, channel_id…
    updated_at        timestamptz DEFAULT now() NOT NULL,
    UNIQUE (platform, label)
);

CREATE TABLE IF NOT EXISTS social_outbox (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    todoist_task_id text,              -- provenance; NULL for API-created posts later
    account_id      bigint NOT NULL REFERENCES social_accounts(id),
    payload         jsonb NOT NULL,    -- {text, link, media_refs…} — connector renders per platform
    status          text DEFAULT 'pending' NOT NULL,  -- pending | posted | failed
    attempt_count   integer DEFAULT 0 NOT NULL,
    last_attempt_at timestamptz,
    posted_ref      text,              -- platform post id/url after success
    created_at      timestamptz DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS social_outbox_pending_idx
    ON social_outbox (created_at) WHERE status = 'pending';

-- One outbox row per (task, account): makes enqueue retries idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS social_outbox_task_account_idx
    ON social_outbox (todoist_task_id, account_id) WHERE todoist_task_id IS NOT NULL;

-- Ships disabled — same kill-switch convention as todoist_capture_enabled.
-- Label names are settings, not hardcoded (config is edited, not coded).
INSERT INTO settings (key, value) VALUES
    ('social_publishing_enabled', 'false'::jsonb),
    ('social_publish_label',      '"publish"'::jsonb),
    ('social_platform_labels',    '{"x":"x","linkedin":"linkedin","facebook":"facebook","youtube":"youtube"}'::jsonb)
    ON CONFLICT (key) DO NOTHING;
