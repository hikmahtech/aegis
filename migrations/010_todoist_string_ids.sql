-- Migration 010: Todoist v1 uses string IDs everywhere
--
-- Migration 009 assumed numeric BIGINT IDs (true under Sync API v9).
-- Sync API v1 (the only working endpoint as of 2025-Q3) returns opaque
-- string IDs like '6CrfhM6VCqCcQXPv'. Cast on insert fails.
--
-- No production data exists yet (sync never succeeded under v9 schema),
-- so we DROP + CREATE rather than ALTER TYPE.

BEGIN;

-- Drop in FK-safe order
DROP TABLE IF EXISTS todoist_outbox CASCADE;
DROP TABLE IF EXISTS todoist_webhook_events CASCADE;
DROP TABLE IF EXISTS todoist_tasks CASCADE;
DROP TABLE IF EXISTS todoist_labels CASCADE;
DROP TABLE IF EXISTS todoist_projects CASCADE;
DROP TABLE IF EXISTS todoist_sync_state CASCADE;

CREATE TABLE todoist_projects (
  id          TEXT PRIMARY KEY,
  parent_id   TEXT REFERENCES todoist_projects(id),
  name        TEXT NOT NULL,
  is_managed  BOOLEAN NOT NULL DEFAULT FALSE,
  is_archived BOOLEAN NOT NULL DEFAULT FALSE,
  order_idx   INT,
  raw         JSONB NOT NULL DEFAULT '{}',
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON todoist_projects (is_managed) WHERE is_managed;
CREATE INDEX ON todoist_projects (parent_id);

CREATE TABLE todoist_labels (
  id    TEXT PRIMARY KEY,
  name  TEXT NOT NULL,
  color TEXT,
  raw   JSONB NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX ON todoist_labels (name);

CREATE TABLE todoist_tasks (
  id             TEXT PRIMARY KEY,
  project_id     TEXT REFERENCES todoist_projects(id),
  parent_id      TEXT REFERENCES todoist_tasks(id),
  content        TEXT NOT NULL,
  description    TEXT,
  due_date       DATE,
  priority       SMALLINT,
  labels         TEXT[] NOT NULL DEFAULT '{}',
  is_completed   BOOLEAN NOT NULL DEFAULT FALSE,
  completed_at   TIMESTAMPTZ,
  assignee_label TEXT,
  source_tag     TEXT,
  raw            JSONB NOT NULL DEFAULT '{}',
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON todoist_tasks (project_id, is_completed);
CREATE INDEX ON todoist_tasks (assignee_label) WHERE NOT is_completed;
CREATE INDEX ON todoist_tasks (source_tag);
CREATE INDEX ON todoist_tasks (due_date) WHERE NOT is_completed;

CREATE TABLE todoist_sync_state (
  key                   TEXT PRIMARY KEY,
  sync_token            TEXT NOT NULL,
  last_full_sync_at     TIMESTAMPTZ,
  last_incremental_at   TIMESTAMPTZ
);
INSERT INTO todoist_sync_state (key, sync_token) VALUES ('main', '*');

CREATE TABLE todoist_outbox (
  id              BIGSERIAL PRIMARY KEY,
  temp_id         TEXT NOT NULL UNIQUE,
  command         JSONB NOT NULL,
  attempt_count   INT NOT NULL DEFAULT 0,
  last_attempt_at TIMESTAMPTZ,
  status          TEXT NOT NULL DEFAULT 'pending',  -- pending | committed | failed
  committed_id    TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON todoist_outbox (status, created_at);

CREATE TABLE todoist_webhook_events (
  id          BIGSERIAL PRIMARY KEY,
  event_name  TEXT NOT NULL,
  event_data  JSONB NOT NULL,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON todoist_webhook_events (event_name, received_at DESC);

COMMIT;
