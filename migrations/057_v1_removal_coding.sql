-- 057: the development lane left v1 (it moved to the v2 Development vertical,
-- a2-development), and with it the repo registry and the infra registry. The
-- code is gone; this deletes the rows only that code read and strips the
-- config that pointed at it.
--
-- Idempotent: a re-run, or a run on a fresh database, changes nothing.
-- Tables are NOT dropped here (`work_sessions`, `infra`, `pending_prs` and the
-- `resources.infra_id` column); a later PR drops them after a dump. The
-- `integration:*` rows of removed keys are kept, as 055 and 056 kept theirs:
-- boot skips a stored key with no registry entry.

-- The repo registry: every `repository` resource. WorkspaceRepoSyncFlow wrote
-- them and the coding lane read them; nothing reads them now. The other kinds
-- (connector, runbook, endpoint, mcp_server) stay.
DELETE FROM resources WHERE kind = 'repository';

-- Todoist project name -> GitHub repo, the coding lane's first repo guess.
DELETE FROM settings WHERE key = 'project_repo_map';

-- The chat tools that went with the lane and the registries. 054 already
-- stripped all but `comment_on_task` from every tool_set; this repeats it for
-- a grant made since. Order is kept; an agent that lists none is not touched.
UPDATE agents
SET metadata = jsonb_set(
        metadata,
        '{tool_set}',
        (
            SELECT COALESCE(jsonb_agg(t.value ORDER BY t.ord), '[]'::jsonb)
            FROM jsonb_array_elements(metadata->'tool_set') WITH ORDINALITY AS t(value, ord)
            WHERE NOT (t.value ?| ARRAY[
                'dispatch_agent_run', 'stop_agent_run', 'list_coding_sessions',
                'task_context', 'report_progress', 'comment_on_task',
                'list_nodes', 'list_services', 'inspect_service', 'get_service_logs',
                'restart_service', 'list_pods', 'list_deployments', 'get_pod_logs',
                'restart_deployment', 'list_argocd_apps', 'sync_argocd_app',
                'list_cloud_accounts', 'cloud_identity', 'run_infra_script'
            ])
        )
    )
WHERE jsonb_typeof(metadata->'tool_set') = 'array'
  AND metadata->'tool_set' ?| ARRAY[
      'dispatch_agent_run', 'stop_agent_run', 'list_coding_sessions',
      'task_context', 'report_progress', 'comment_on_task',
      'list_nodes', 'list_services', 'inspect_service', 'get_service_logs',
      'restart_service', 'list_pods', 'list_deployments', 'get_pod_logs',
      'restart_deployment', 'list_argocd_apps', 'sync_argocd_app',
      'list_cloud_accounts', 'cloud_identity', 'run_infra_script'
  ];

-- The agent-task sweep's coding knobs (054 set `max_coding` to 0).
UPDATE activities
SET config = config - 'max_coding' - 'turn_timeout_minutes', updated_at = now()
WHERE workflow_type = 'AgentTaskSweepFlow'
  AND (config ? 'max_coding' OR config ? 'turn_timeout_minutes');

-- CleanupFlow no longer releases coding-session worktrees.
UPDATE activities
SET config = config - 'task_session_days', updated_at = now()
WHERE workflow_type = 'CleanupFlow'
  AND config ? 'task_session_days';
