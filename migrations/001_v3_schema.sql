-- AEGIS v3 fresh schema.
-- Copied verbatim from docs/superpowers/specs/2026-04-17-aegis-v3-redesign.md §8.
-- No data preservation constraint: v2 migrations are deleted and DB is reset
-- before this file runs. Section dividers preserved as SQL comments.

-- =====================================================================
-- Public: housekeeping
-- =====================================================================

CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =====================================================================
-- Public: primitives
-- =====================================================================

CREATE TABLE agents (
  id                           TEXT PRIMARY KEY,
  name                         TEXT NOT NULL,
  role                         TEXT NOT NULL,
  system_prompt_path           TEXT NOT NULL,
  capabilities                 JSONB NOT NULL DEFAULT '[]',
  model_tier                   TEXT NOT NULL DEFAULT 'balanced',
  interaction_timeout_default  TEXT NOT NULL DEFAULT 'archive',
  telegram_topic_id            INT,
  active                       BOOLEAN NOT NULL DEFAULT TRUE,
  created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE activities (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  slug             TEXT UNIQUE NOT NULL,
  workflow_type    TEXT NOT NULL,
  agent_id         TEXT NOT NULL REFERENCES agents(id),
  schedule_cron    TEXT NOT NULL,
  config           JSONB NOT NULL DEFAULT '{}',
  active           BOOLEAN NOT NULL DEFAULT TRUE,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON activities (agent_id) WHERE active;

CREATE TABLE interactions (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  flow_run_id          TEXT NOT NULL,
  agent_id             TEXT NOT NULL REFERENCES agents(id),
  kind                 TEXT NOT NULL,
  origin               TEXT NOT NULL,
  prompt               TEXT NOT NULL,
  options              JSONB,
  status               TEXT NOT NULL DEFAULT 'pending',
  response             JSONB,
  timeout_at           TIMESTAMPTZ,
  timeout_policy       TEXT NOT NULL DEFAULT 'archive',
  telegram_message_id  BIGINT,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at          TIMESTAMPTZ
);
CREATE INDEX ON interactions (agent_id, status, created_at DESC);
CREATE INDEX ON interactions (flow_run_id) WHERE status = 'pending';
CREATE INDEX ON interactions (origin, created_at DESC);

CREATE TABLE workflow_runs (
  run_id           TEXT PRIMARY KEY,
  workflow_id      TEXT NOT NULL,
  workflow_type    TEXT NOT NULL,
  agent_id         TEXT REFERENCES agents(id),
  parent_run_id    TEXT,
  status           TEXT NOT NULL,
  started_at       TIMESTAMPTZ NOT NULL,
  completed_at     TIMESTAMPTZ,
  duration_ms      INT,
  error            TEXT,
  input_summary    JSONB,
  result_summary   JSONB
);
CREATE INDEX ON workflow_runs (agent_id, started_at DESC);
CREATE INDEX ON workflow_runs (workflow_type, started_at DESC);
CREATE INDEX ON workflow_runs (status) WHERE status = 'running';

-- =====================================================================
-- Public: config + reference
-- =====================================================================

CREATE TABLE settings (
  key        TEXT PRIMARY KEY,
  value      JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE channels (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind       TEXT NOT NULL,                  -- email | telegram | webhook | rss | raindrop
  identifier TEXT NOT NULL,                  -- email addr / chat_id / feed url / …
  config     JSONB NOT NULL DEFAULT '{}',    -- per-channel state (account_label, token path, last_cursor, filters)
  active     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (kind, identifier)
);

CREATE TABLE resources (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind        TEXT NOT NULL,                 -- connector | runbook | endpoint | mcp_server
  slug        TEXT UNIQUE NOT NULL,
  title       TEXT NOT NULL,
  content     TEXT,
  url         TEXT,
  tags        TEXT[] NOT NULL DEFAULT '{}',
  metadata    JSONB NOT NULL DEFAULT '{}',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON resources USING gin (tags);

-- =====================================================================
-- Public: observability
-- =====================================================================

CREATE TABLE llm_calls (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id        TEXT REFERENCES agents(id),
  workflow_run_id TEXT REFERENCES workflow_runs(run_id),
  model           TEXT NOT NULL,
  purpose         TEXT,
  input_tokens    INT,
  output_tokens   INT,
  latency_ms      INT,
  status          TEXT NOT NULL,
  error           TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON llm_calls (agent_id, created_at DESC);
CREATE INDEX ON llm_calls (workflow_run_id);

CREATE TABLE connector_calls (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id        TEXT REFERENCES agents(id),
  workflow_run_id TEXT REFERENCES workflow_runs(run_id),
  connector       TEXT NOT NULL,
  action          TEXT NOT NULL,
  status          TEXT NOT NULL,
  latency_ms      INT,
  external_ref    TEXT,
  error           TEXT,
  metadata        JSONB,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON connector_calls (agent_id, created_at DESC);
CREATE INDEX ON connector_calls (connector, created_at DESC);

CREATE TABLE chat_tool_calls (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id        TEXT NOT NULL REFERENCES agents(id),
  tool_name       TEXT NOT NULL,
  args            JSONB,
  result          JSONB,
  status          TEXT NOT NULL,
  latency_ms      INT,
  error           TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON chat_tool_calls (agent_id, created_at DESC);
CREATE INDEX ON chat_tool_calls (tool_name, created_at DESC);

CREATE TABLE audit_log (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  actor       TEXT NOT NULL,
  action      TEXT NOT NULL,
  target_type TEXT,
  target_id   TEXT,
  details     JSONB,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON audit_log (created_at DESC);
CREATE INDEX ON audit_log (actor, created_at DESC);

CREATE TABLE chat_history (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  thread_id   TEXT NOT NULL,
  agent_id    TEXT NOT NULL REFERENCES agents(id),
  role        TEXT NOT NULL,
  content     TEXT,
  metadata    JSONB,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON chat_history (thread_id, created_at);
CREATE INDEX ON chat_history (agent_id, created_at DESC);

-- =====================================================================
-- Public: knowledge governance (thin, AEGIS-only)
-- =====================================================================

CREATE TABLE knowledge_injection_log (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id        TEXT NOT NULL REFERENCES agents(id),
  thread_id       TEXT,
  workflow_run_id TEXT REFERENCES workflow_runs(run_id),
  source          TEXT NOT NULL,
  content_ids     TEXT[] NOT NULL DEFAULT '{}',
  triples_used    JSONB,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON knowledge_injection_log (agent_id, created_at DESC);

CREATE TABLE knowledge_source_quality (
  source_type       TEXT PRIMARY KEY,
  auto_confidence   NUMERIC NOT NULL DEFAULT 0.7,
  review_confidence NUMERIC NOT NULL DEFAULT 0.3,
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =====================================================================
-- Public: ingestion / triage (de-coupled from tasks)
-- =====================================================================

CREATE TABLE triage_state (
  email_addr  TEXT PRIMARY KEY,
  state       TEXT NOT NULL,
  metadata    JSONB NOT NULL DEFAULT '{}',
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE triage_accuracy (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email_id       TEXT NOT NULL,
  predicted      TEXT NOT NULL,
  actual         TEXT,
  corrected_by   TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON triage_accuracy (created_at DESC);

CREATE TABLE ingest_idempotency (
  source_type  TEXT NOT NULL,
  external_id  TEXT NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (source_type, external_id)
);

-- =====================================================================
-- Domain: maou.*
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS maou;

CREATE TABLE maou.recurring_charge (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  identity_hash TEXT UNIQUE NOT NULL,
  merchant      TEXT NOT NULL,
  account       TEXT NOT NULL,
  amount        NUMERIC,
  currency      TEXT,
  cadence       TEXT,
  first_seen    TIMESTAMPTZ NOT NULL,
  last_seen     TIMESTAMPTZ NOT NULL,
  next_expected TIMESTAMPTZ,
  status        TEXT NOT NULL DEFAULT 'active',
  metadata      JSONB NOT NULL DEFAULT '{}',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE maou.receipt_email (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  message_id   TEXT UNIQUE NOT NULL,
  account      TEXT NOT NULL,
  sender       TEXT NOT NULL,
  subject      TEXT,
  received_at  TIMESTAMPTZ NOT NULL,
  charge_id    UUID REFERENCES maou.recurring_charge(id),
  parsed       JSONB,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON maou.receipt_email (charge_id);

CREATE TABLE maou.renewal_alert (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  charge_id   UUID NOT NULL REFERENCES maou.recurring_charge(id),
  alert_at    TIMESTAMPTZ NOT NULL,
  status      TEXT NOT NULL DEFAULT 'pending',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE maou.subscription_digest (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  period      DATERANGE NOT NULL,
  summary     JSONB NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =====================================================================
-- Domain: pandoras_actor.*
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS pandoras_actor;

CREATE TABLE pandoras_actor.homelab_drift (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  service     TEXT NOT NULL,
  observed    JSONB NOT NULL,
  severity    TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'open',
  detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at TIMESTAMPTZ
);
CREATE INDEX ON pandoras_actor.homelab_drift (service, detected_at DESC);

CREATE TABLE pandoras_actor.backup_health (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  series        TEXT NOT NULL,
  last_bytes    BIGINT,
  delta_pct     NUMERIC,
  status        TEXT NOT NULL,
  observed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON pandoras_actor.backup_health (series, observed_at DESC);

CREATE TABLE pandoras_actor.schedule_health (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  schedule_id   TEXT NOT NULL,
  last_run_at   TIMESTAMPTZ,
  status        TEXT NOT NULL,
  metadata      JSONB,
  observed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE pandoras_actor.cert_expiry (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  domain        TEXT NOT NULL UNIQUE,
  expires_at    TIMESTAMPTZ NOT NULL,
  issuer        TEXT,
  status        TEXT NOT NULL,
  observed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
