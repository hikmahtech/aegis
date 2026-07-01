-- Notification budget (Phase 5). Logs every proactive FYI notification so the
-- budget gate can cap the daily volume (kill the noise). Record-only until
-- AEGIS_NOTIFICATION_BUDGET_ENABLED=true.
CREATE TABLE IF NOT EXISTS notification_log (
    id         BIGSERIAL PRIMARY KEY,
    agent_id   TEXT,
    log_event  TEXT,
    sent       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS notification_log_day_idx ON notification_log (created_at DESC);
