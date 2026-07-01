-- Migration 011: Todoist capture idempotency + kill switch
--
-- Phase 2 of the GTD+Todoist integration (see spec
-- docs/superpowers/specs/2026-05-19-gtd-todoist-phase2-capture-design.md).
-- One row per (source_tag, external_id) prevents duplicate Inbox tasks
-- when the same source artifact is fetched twice.

BEGIN;

CREATE TABLE todoist_capture_idempotency (
    source_tag       TEXT NOT NULL,
    external_id      TEXT NOT NULL,
    todoist_task_ref TEXT,
    captured_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_tag, external_id)
);
CREATE INDEX ON todoist_capture_idempotency (captured_at);

-- Kill switch — seed only if not already present. Default is enabled.
INSERT INTO settings (key, value)
VALUES ('todoist_capture_enabled', 'true'::jsonb)
ON CONFLICT (key) DO NOTHING;

COMMIT;
