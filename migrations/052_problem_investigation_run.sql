-- Which investigation run holds a problem (issue #639).
--
-- A resolve and a new firing 0.6 s apart reopened a problem while its first
-- `AlertInvestigationFlow` was still running, and the reopen started a second
-- one. The two runs then told Slack opposite things ("stand down", then
-- "actionable") and the second opened a PR for a problem the first had
-- already resolved.
--
-- A run claims the problem when it starts by writing its workflow id here
-- (`hub.claim_investigation`). A later run takes the claim over only when the
-- holder is no longer running, which the worker asks Temporal. Nothing clears
-- the column when a run ends: a failed or cancelled run writes nothing
-- either, so "is the holder still running?" is the only test that is true
-- for every way a run can end.
--
-- Idempotent. NULL means no run has claimed the problem, which is every row
-- that exists when this runs.

ALTER TABLE problems ADD COLUMN IF NOT EXISTS investigation_run text;
