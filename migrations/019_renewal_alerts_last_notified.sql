-- 019_renewal_alerts_last_notified.sql — Telegram-level dedup for renewal alerts.
--
-- The DB-level partial unique index on
--   (charge_id, threshold_days, ((fired_at AT TIME ZONE 'UTC')::date))
-- already dedups the *Inbox capture* side (one alert row per UTC day per
-- (charge, threshold)). But `notify_renewal_alert` reads new alert rows
-- straight out of `evaluate_renewal_alerts` and sends Telegram — so any
-- time evaluate inserts a fresh alert (e.g. a different threshold on the
-- same charge, or a charge that just crossed its first threshold), the
-- user gets pinged again. For past-due charges (days_left < 0 against
-- threshold 0) this can repeat daily.
--
-- This migration adds a `last_notified_at` column to maou.renewal_alert.
-- `notify_renewal_alert` checks for a notification within the last 7d on
-- the SAME (charge_id, threshold_days) and skips the send if found.
-- After a successful send it stamps `last_notified_at = NOW()` on the
-- row whose id is in the alert payload.

ALTER TABLE maou.renewal_alert
  ADD COLUMN IF NOT EXISTS last_notified_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS renewal_alert_notify_lookup_idx
  ON maou.renewal_alert (charge_id, threshold_days, last_notified_at);
