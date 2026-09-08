-- `pending_prs` learns the problem, and forgets the fingerprint.
--
-- The row was keyed on `alert_fingerprint`, written on every staged PR and
-- read by nothing: the only SELECT reads title/body/branch by id. It is the
-- last place an alert's identity was carried as a fingerprint rather than as
-- the problem it belongs to, which is the duplication the problem hub exists
-- to remove.
--
-- `problem_id` replaces it, so a staged PR says which problem it is a fix for
-- before it is opened. Once opened, `record_investigation` already links the
-- PR url to the same problem (`problem_links(link_kind='github_pr')`).
--
-- Nullable and ON DELETE SET NULL: a pending PR outlives the sweep that
-- closes its problem, and losing the link is better than blocking the close.
ALTER TABLE pending_prs ADD COLUMN IF NOT EXISTS problem_id uuid
    REFERENCES problems(id) ON DELETE SET NULL;
ALTER TABLE pending_prs DROP COLUMN IF EXISTS alert_fingerprint;

CREATE INDEX IF NOT EXISTS pending_prs_problem ON pending_prs (problem_id)
    WHERE problem_id IS NOT NULL;
