-- work_sessions: the registry of who is on which task (problem hub, PR 5).
--
-- Widens `task_sessions` (migration 025) from "one AEGIS coding session per
-- task" to one row per session on a task, AEGIS's or the operator's. The
-- rename is guarded so this file is safe to re-run: `schema_migrations` keys
-- on the filename, and a renumbered migration runs again.
DO $$
BEGIN
    IF to_regclass('public.task_sessions') IS NOT NULL
       AND to_regclass('public.work_sessions') IS NULL THEN
        ALTER TABLE task_sessions RENAME TO work_sessions;
    END IF;
END $$;

ALTER TABLE work_sessions DROP CONSTRAINT IF EXISTS task_sessions_pkey;
ALTER TABLE work_sessions ADD COLUMN IF NOT EXISTS id uuid NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE work_sessions ADD COLUMN IF NOT EXISTS problem_id uuid REFERENCES problems(id) ON DELETE SET NULL;
-- The CLAUDE_CONFIG_DIR account label the session runs under. `--resume`
-- reads it back instead of re-resolving, so a later turn cannot land on the
-- wrong profile.
ALTER TABLE work_sessions ADD COLUMN IF NOT EXISTS account text NOT NULL DEFAULT '';
ALTER TABLE work_sessions ADD COLUMN IF NOT EXISTS engine text NOT NULL DEFAULT 'claude';
ALTER TABLE work_sessions ADD COLUMN IF NOT EXISTS owner text NOT NULL DEFAULT 'aegis';    -- aegis | operator
ALTER TABLE work_sessions ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active';  -- active | parked | done
ALTER TABLE work_sessions ADD COLUMN IF NOT EXISTS summary text NOT NULL DEFAULT '';
ALTER TABLE work_sessions ADD COLUMN IF NOT EXISTS last_seen_at timestamptz;
UPDATE work_sessions SET last_seen_at = COALESCE(last_turn_at, created_at) WHERE last_seen_at IS NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'work_sessions'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE work_sessions ADD PRIMARY KEY (id);
    END IF;
END $$;

-- One live AEGIS session per task: `create_session`'s ON CONFLICT targets
-- this index, which is what keeps two comments arriving together from
-- forking a task into two conversations. Operator rows are not bounded.
CREATE UNIQUE INDEX IF NOT EXISTS work_sessions_task_aegis
    ON work_sessions (task_id) WHERE owner = 'aegis' AND status <> 'done';
CREATE INDEX IF NOT EXISTS work_sessions_task ON work_sessions (task_id);
CREATE INDEX IF NOT EXISTS work_sessions_problem ON work_sessions (problem_id) WHERE problem_id IS NOT NULL;
