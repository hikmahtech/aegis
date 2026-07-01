-- Migration 003: reshape pandoras_actor.* homelab tables to match the
-- HomelabActivities worker writers (service_drift, schedule_health,
-- cert_radar, backup_audit flows) and the /api/admin/homelab/state reader.
--
-- Migration 001 shipped minimal stubs (service/observed/series/...) but
-- the worker + core were written against a richer design that was never
-- migrated. Result: worker INSERTs and core SELECTs have been failing with
-- UndefinedColumnError since the v3 cutover. All four tables are empty in
-- production, so DROP+CREATE is safe.

DROP TABLE IF EXISTS pandoras_actor.homelab_drift CASCADE;
DROP TABLE IF EXISTS pandoras_actor.backup_health CASCADE;
DROP TABLE IF EXISTS pandoras_actor.schedule_health CASCADE;
DROP TABLE IF EXISTS pandoras_actor.cert_expiry CASCADE;

CREATE TABLE pandoras_actor.homelab_drift (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  service_name  TEXT NOT NULL,
  stack_name    TEXT NOT NULL,
  drift_type    TEXT NOT NULL,
  expected      JSONB NOT NULL,
  actual        JSONB NOT NULL,
  severity      TEXT NOT NULL,
  alert_key     TEXT NOT NULL,
  detected_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at   TIMESTAMPTZ
);
CREATE UNIQUE INDEX homelab_drift_open_alert_key_idx
  ON pandoras_actor.homelab_drift (alert_key)
  WHERE resolved_at IS NULL;
CREATE INDEX homelab_drift_service_detected_at_idx
  ON pandoras_actor.homelab_drift (service_name, detected_at DESC);

CREATE TABLE pandoras_actor.backup_health (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  backup_set        TEXT NOT NULL,
  last_backup_at    TIMESTAMPTZ,
  size_bytes        BIGINT,
  size_delta_pct    NUMERIC,
  restore_drill_at  TIMESTAMPTZ,
  restore_drill_ok  BOOLEAN,
  restore_drill_ms  INTEGER,
  notes             TEXT,
  checked_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX backup_health_set_checked_idx
  ON pandoras_actor.backup_health (backup_set, checked_at DESC);

CREATE TABLE pandoras_actor.schedule_health (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source                TEXT NOT NULL,
  schedule_name         TEXT NOT NULL,
  expected_status       TEXT NOT NULL,
  actual_status         TEXT NOT NULL,
  last_run_at           TIMESTAMPTZ,
  last_run_ok           BOOLEAN,
  consecutive_failures  INTEGER NOT NULL DEFAULT 0,
  alert_key             TEXT,
  checked_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source, schedule_name)
);

CREATE TABLE pandoras_actor.cert_expiry (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  domain                TEXT NOT NULL,
  cert_serial           TEXT NOT NULL,
  not_after             TIMESTAMPTZ NOT NULL,
  days_until_expiry     INTEGER NOT NULL,
  last_alert_threshold  INTEGER,
  checked_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (domain, cert_serial)
);
CREATE INDEX cert_expiry_domain_checked_idx
  ON pandoras_actor.cert_expiry (domain, checked_at DESC);
