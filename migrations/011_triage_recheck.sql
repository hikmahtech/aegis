-- Triage feedback loop (#74): recheck_triage_outcomes re-reads Gmail labels
-- for recent unscored predictions. last_checked_at marks that a row was
-- actually observed at least once — only such rows may be implicitly
-- confirmed (actual = predicted) once the correction window closes.
ALTER TABLE triage_accuracy ADD COLUMN IF NOT EXISTS last_checked_at timestamptz;
