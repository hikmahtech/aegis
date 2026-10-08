-- 055: the Sentry, Jira and GitHub intakes left v1 (they moved to the v2
-- DevOps and Development verticals). Their code is gone; this deletes the
-- settings rows only they read.
--
-- Idempotent: a re-run, or a run on a fresh database, deletes nothing.
-- Tables (`pending_prs` and the rest) are NOT dropped here; a later PR does
-- that after a dump. The `integration:*` rows of the removed keys (Sentry,
-- Jira, the GitHub webhook secret) are kept too: boot skips a stored key with
-- no registry entry, and a v2 vertical may still want to copy a credential.

-- SentryPollFlow's cursor.
DELETE FROM settings WHERE key = 'sentry_last_issue_id';
-- The `configure_triage` list of Sentry projects to ignore; nothing reads it.
DELETE FROM settings WHERE key = 'triage_sentry_ignored_projects';
