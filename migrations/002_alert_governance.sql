-- migrations/002_alert_governance.sql
-- Phase 3b: alert approval gates + mute table.

CREATE TABLE IF NOT EXISTS alert_mutes (
    mute_key      TEXT         PRIMARY KEY,
    muted_until   TIMESTAMPTZ  NOT NULL,
    reason        TEXT         NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    created_by    TEXT         NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS alert_mutes_until_idx ON alert_mutes (muted_until);

CREATE TABLE IF NOT EXISTS pending_prs (
    id                 UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    interaction_id     UUID         REFERENCES interactions(id),
    alert_fingerprint  TEXT         NOT NULL,
    repo               TEXT         NOT NULL,
    branch             TEXT         NOT NULL,
    title              TEXT         NOT NULL,
    body               TEXT         NOT NULL,
    diff               TEXT         NOT NULL DEFAULT '',
    kimi_session_id    TEXT         NOT NULL DEFAULT '',
    status             TEXT         NOT NULL DEFAULT 'pending',
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    resolved_at        TIMESTAMPTZ,
    CONSTRAINT pending_prs_status_valid
        CHECK (status IN ('pending','opened','closed','discarded'))
);
CREATE INDEX IF NOT EXISTS pending_prs_status_idx ON pending_prs (status, created_at);
