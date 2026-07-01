-- Migration 006: Reshape maou money tables to match MoneyActivities SQL.
--
-- The v3 stub schema (001) used different column names than the code.
-- All 4 affected tables have been empty since v3 cutover (every INSERT failed
-- with UndefinedColumnError), so DROP + recreate is safe.
--
-- Drop in dependency order (child tables first).

DROP TABLE IF EXISTS maou.subscription_digest;
DROP TABLE IF EXISTS maou.renewal_alert;
DROP TABLE IF EXISTS maou.receipt_email;
DROP TABLE IF EXISTS maou.recurring_charge;

-- Recurring charges — keyed on (account, sender_label, amount_cents, currency).
CREATE TABLE maou.recurring_charge (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account               TEXT NOT NULL,
  sender_label          TEXT NOT NULL,
  vendor_name           TEXT NOT NULL,
  category              TEXT NOT NULL DEFAULT 'other',
  amount_cents          INT  NOT NULL,
  currency              TEXT NOT NULL,
  monthly_inr_equivalent NUMERIC NOT NULL DEFAULT 0,
  cadence               TEXT NOT NULL DEFAULT 'unknown',
  first_seen_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  next_due_at           TIMESTAMPTZ,
  status                TEXT NOT NULL DEFAULT 'active',
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (account, sender_label, amount_cents, currency)
);

-- Receipt emails linked back to a recurring charge after extraction.
CREATE TABLE maou.receipt_email (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  message_id  TEXT UNIQUE NOT NULL,
  account     TEXT NOT NULL,
  sender      TEXT NOT NULL,
  subject     TEXT,
  received_at TIMESTAMPTZ NOT NULL,
  charge_id   UUID REFERENCES maou.recurring_charge(id),
  parsed      JSONB,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ON maou.receipt_email (charge_id);

-- Renewal alerts — one row per (charge, threshold, calendar day) thanks to
-- the partial unique index used by the ON CONFLICT DO NOTHING dedup.
CREATE TABLE maou.renewal_alert (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  charge_id      UUID NOT NULL REFERENCES maou.recurring_charge(id),
  threshold_days INT  NOT NULL,
  fired_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX renewal_alert_daily_dedup
  ON maou.renewal_alert (charge_id, threshold_days, ((fired_at AT TIME ZONE 'UTC')::date));

-- Monthly subscription digest — one row per calendar-month window.
CREATE TABLE maou.subscription_digest (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  period_start DATE NOT NULL,
  period_end   DATE NOT NULL,
  summary      JSONB NOT NULL,
  sent_at      TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (period_start, period_end)
);
