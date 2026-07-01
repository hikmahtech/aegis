-- Migration 009: Todoist projection + outbox + webhook audit
--
-- AEGIS holds a READ-ONLY projection of Todoist's project/task/label graph.
-- The source of truth is Todoist; this schema is purely for fast local reads
-- and outage-tolerant writes. See spec
-- docs/superpowers/specs/2026-05-18-gtd-todoist-integration-design.md §4.

BEGIN;

CREATE TABLE todoist_projects (
  id          BIGINT PRIMARY KEY,
  parent_id   BIGINT REFERENCES todoist_projects(id),
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
  id    BIGINT PRIMARY KEY,
  name  TEXT NOT NULL,
  color TEXT,
  raw   JSONB NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX ON todoist_labels (name);

CREATE TABLE todoist_tasks (
  id             BIGINT PRIMARY KEY,
  project_id     BIGINT REFERENCES todoist_projects(id),
  parent_id      BIGINT REFERENCES todoist_tasks(id),
  content        TEXT NOT NULL,
  description    TEXT,
  due_date       DATE,
  priority       SMALLINT,
  labels         TEXT[] NOT NULL DEFAULT '{}',
  is_completed   BOOLEAN NOT NULL DEFAULT FALSE,
  completed_at   TIMESTAMPTZ,
  assignee_label TEXT,            -- computed: first @me/@<personality> label seen
  source_tag     TEXT,            -- computed: first #<source> hashtag seen
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
  committed_id    BIGINT,
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

ALTER TABLE workflow_runs ADD COLUMN todoist_task_ref TEXT;
CREATE INDEX ON workflow_runs (todoist_task_ref) WHERE todoist_task_ref IS NOT NULL;

COMMIT;
