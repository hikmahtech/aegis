-- Per-alert runbooks, stored as data (#499).
--
-- Pandora puts a runbook in front of every alert investigation. The generic
-- ones ship as files in the repo's runbooks/ directory, baked into the worker
-- image. A runbook about one deployment's own setup (which machines share a
-- power supply, which service is pinned to which node) cannot go there: the
-- repo is public, and a fork must not inherit one operator's topology. It goes
-- in this table instead. The worker reads this table first and falls back to
-- the file (services/runbooks.py, AlertActivities._read_runbook).
--
-- `name_key` is the alert name folded to lowercase letters and digits
-- (runbooks.normalise_name), so "Dagster Pipeline Failure",
-- "dagster-pipeline-failure" and "DagsterPipelineFailure" are one row. The
-- CHECK holds a hand-written INSERT to the same rule: a key the service would
-- never produce is a row no lookup can reach, and that should fail loudly.
-- `name` keeps the spelling last saved, for display.
--
-- Idempotent DDL: the migration runner keys on the filename and re-runs a
-- renamed file.
CREATE TABLE IF NOT EXISTS runbooks (
    name_key   text PRIMARY KEY CHECK (name_key ~ '^[a-z0-9]+$'),
    name       text NOT NULL,
    body       text NOT NULL CHECK (body ~ '\S'),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL DEFAULT ''
);
