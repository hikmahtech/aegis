-- task_sessions: one persistent Claude Code session per @code Todoist task.
-- The comment thread on the task is the control channel; this row is what
-- lets a later comment resume the same session in the same worktree.
CREATE TABLE IF NOT EXISTS task_sessions (
    task_id       text PRIMARY KEY,
    agent_id      text NOT NULL,
    session_id    uuid NOT NULL,
    repo          text NOT NULL DEFAULT '',   -- workspace-relative checkout path; '' = not yet resolved
    github_repo   text NOT NULL DEFAULT '',
    worktree_path text NOT NULL DEFAULT '',
    branch        text NOT NULL DEFAULT '',
    host          text NOT NULL DEFAULT '',
    -- The last turn AEGIS launched. Its liveness is the ONLY way to tell an
    -- orphaned turn of ours from an operator who took the session over: both
    -- sit in the same `-aegis-wt/` worktree, which is what the take-over footer
    -- tells them to `cd` into, so the session registry tags both `aegis`.
    last_output_file text NOT NULL DEFAULT '',
    last_host        text NOT NULL DEFAULT '',
    slack_ref     jsonb,                      -- {"channel","ts"} thread root; NULL until first delivery
    turns         int  NOT NULL DEFAULT 0,
    last_turn_at  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);
