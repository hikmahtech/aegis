-- migrations/012_gtd_clarify.sql
-- Phase 3 of the GTD+Todoist integration. See
-- docs/superpowers/specs/2026-05-19-gtd-todoist-phase3-clarify-design.md.
--
-- Adds:
--   - gtd_clarify_log: one row per classify_one decision (incl. user-comment
--     re-runs and low-confidence no-apply rows).
--   - todoist_notes: projection of Todoist task comments so ClarifyFlow can
--     read user supervision.
--   - todoist_tasks.last_clarified_at / last_note_at: ClarifyFlow's
--     watermark + comment-loop trigger.
--   - settings seeds: kill switches and user_timezone for the 2-min gate.

BEGIN;

CREATE TABLE gtd_clarify_log (
    id                BIGSERIAL PRIMARY KEY,
    todoist_task_id   TEXT NOT NULL,
    pass              INT NOT NULL DEFAULT 1,
    source_tag        TEXT,
    classification    TEXT NOT NULL,
    confidence        REAL NOT NULL,
    assignee          TEXT,
    contexts          TEXT[] NOT NULL DEFAULT '{}',
    reason            TEXT,
    user_hint         TEXT,
    llm_model         TEXT NOT NULL,
    prompt_tokens     INT,
    completion_tokens INT,
    latency_ms        INT,
    applied           BOOLEAN NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX gtd_clarify_log_task_pass_idx ON gtd_clarify_log (todoist_task_id, pass);
CREATE INDEX gtd_clarify_log_created_at_idx ON gtd_clarify_log (created_at);

CREATE TABLE todoist_notes (
    id         TEXT PRIMARY KEY,
    item_id    TEXT NOT NULL REFERENCES todoist_tasks(id),
    content    TEXT NOT NULL,
    posted_uid TEXT,
    posted_at  TIMESTAMPTZ NOT NULL,
    raw        JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX todoist_notes_item_posted_idx ON todoist_notes (item_id, posted_at DESC);

ALTER TABLE todoist_tasks
    ADD COLUMN last_clarified_at TIMESTAMPTZ,
    ADD COLUMN last_note_at      TIMESTAMPTZ;

CREATE INDEX todoist_tasks_unclarified_idx
    ON todoist_tasks (last_clarified_at)
    WHERE last_clarified_at IS NULL;
CREATE INDEX todoist_tasks_note_at_idx
    ON todoist_tasks (project_id, last_note_at)
    WHERE last_note_at IS NOT NULL;

-- Kill switches + timezone seed. ON CONFLICT keeps prior operator overrides.
INSERT INTO settings (key, value) VALUES
    ('gtd_clarify_enabled',    'true'::jsonb),
    ('gtd_2min_rule_enabled',  'true'::jsonb),
    ('user_timezone',          '"Asia/Kolkata"'::jsonb)
ON CONFLICT (key) DO NOTHING;

COMMIT;
