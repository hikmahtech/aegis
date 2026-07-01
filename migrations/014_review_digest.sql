-- Migration 014: review_digest_log audit table for Phase 5 reviews
--
-- Phase 5 of the GTD+Todoist integration. See
-- docs/superpowers/specs/2026-05-20-gtd-todoist-phase5-reviews-design.md.
--
-- DailyReviewFlow + WeeklyReviewFlow build a digest of inbox/today/
-- waiting-for/clarify state, send a Telegram card, and spawn an
-- abandoned InteractionFlow child for the user to acknowledge. The
-- log row tracks both the digest counts and the user's response.

BEGIN;

CREATE TABLE review_digest_log (
    id              BIGSERIAL PRIMARY KEY,
    review_kind     TEXT NOT NULL,                  -- 'daily' | 'weekly'
    counts          JSONB NOT NULL DEFAULT '{}',
    preview         TEXT,
    interaction_id  TEXT,
    user_choice     TEXT,                            -- null until user acks
    acknowledged    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    acknowledged_at TIMESTAMPTZ
);
CREATE INDEX review_digest_log_kind_created_idx
    ON review_digest_log (review_kind, created_at DESC);
CREATE INDEX review_digest_log_interaction_idx
    ON review_digest_log (interaction_id)
    WHERE interaction_id IS NOT NULL;

COMMIT;
