-- Migration 018: drop UNIQUE(name) on todoist_labels
--
-- The previous unique index (migrations 009:29 and 010:39) is a sync-token
-- poison pill: a Todoist diff containing a rename collision (label A id=X
-- name="Old" already in our projection; label B id=Y name="Old" arrives in
-- the same delta) makes `INSERT ... ON CONFLICT (id) DO UPDATE` violate
-- the name-unique index because the INSERT path matches by id, not name.
-- The whole transaction aborts, sync_token never advances, and every
-- subsequent TodoistSyncFlow tick re-polls the same poison diff. New
-- Jira-synced APP- tasks never reach ClarifyFlow.
--
-- No runtime code reads todoist_labels by name (the seed bootstrap uses
-- ids; chat-tool reads project labels off todoist_tasks.labels arrays).
-- Dropping the index is safe.

BEGIN;

-- Index name is auto-generated; both migrations created it with the same
-- expression so PG assigns `todoist_labels_name_idx`. Drop defensively.
DROP INDEX IF EXISTS todoist_labels_name_idx;

COMMIT;
