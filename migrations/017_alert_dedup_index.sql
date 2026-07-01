-- 017_alert_dedup_index.sql — coarse-grained alert dedup beyond fingerprint.
--
-- check_dedup (audit_log) matches the alert fingerprint exactly. Sentry mints
-- a fresh issue id for every stack-frame variation of the same underlying
-- error, so seven IncompatiblePeer events in five hours produced seven
-- separate Todoist tasks. This table maps a coarser "signature"
-- (source + service + error class) to the open Todoist task that owns the
-- cluster, so AlertInvestigationFlow can attach recurrences to the existing
-- task instead of spawning a duplicate investigation.

CREATE TABLE IF NOT EXISTS alert_dedup_index (
    signature        TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    occurrence_count INTEGER     NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS alert_dedup_index_task_id_idx
    ON alert_dedup_index (task_id);
