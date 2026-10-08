-- 054: prepare the v1 removal. The infra lane (now the DevOps vertical), the
-- development lane with Sentry and Jira, and the books money lane leave v1 in
-- the PRs after this one. This migration only takes their schedules, tools and
-- agent out of service; their code is deleted later.
--
-- Every statement is idempotent: a re-run, or a run on a fresh database, is a
-- no-op. The seed YAML carries the same changes, so `load_seeds` (which runs
-- after migrations) does not undo them.

-- 1. Strip the tools the removal PRs delete from every agent's tool_set.
--    `metadata` is DB-owned once it exists (`seed.py` merges the YAML under
--    it), so editing the YAML alone would change nothing on a live deployment.
--    Order is kept; an agent that lists none of these is not touched.
UPDATE agents
SET metadata = jsonb_set(
        metadata,
        '{tool_set}',
        (
            SELECT COALESCE(jsonb_agg(t.value ORDER BY t.ord), '[]'::jsonb)
            FROM jsonb_array_elements(metadata->'tool_set') WITH ORDINALITY AS t(value, ord)
            WHERE NOT (t.value ?| ARRAY[
                -- infra tools
                'list_nodes', 'list_services', 'inspect_service', 'get_service_logs',
                'restart_service', 'list_pods', 'list_deployments', 'get_pod_logs',
                'list_argocd_apps', 'sync_argocd_app', 'restart_deployment',
                'list_cloud_accounts', 'cloud_identity', 'run_infra_script',
                -- development lane and the hub's infra helpers
                'dispatch_agent_run', 'stop_agent_run', 'list_coding_sessions',
                'task_context', 'report_progress', 'investigate_resource',
                'aegis_self_diagnose', 'update_runbook', 'set_service_state',
                -- vercel
                'vercel_get_project', 'vercel_list_deployments', 'vercel_get_deployment',
                'vercel_get_build_logs',
                -- the books
                'ledger_query', 'ledger_post', 'ledger_reclassify', 'ledger_add_rule'
            ])
        )
    )
WHERE jsonb_typeof(metadata->'tool_set') = 'array'
  AND metadata->'tool_set' ?| ARRAY[
      'list_nodes', 'list_services', 'inspect_service', 'get_service_logs',
      'restart_service', 'list_pods', 'list_deployments', 'get_pod_logs',
      'list_argocd_apps', 'sync_argocd_app', 'restart_deployment',
      'list_cloud_accounts', 'cloud_identity', 'run_infra_script',
      'dispatch_agent_run', 'stop_agent_run', 'list_coding_sessions',
      'task_context', 'report_progress', 'investigate_resource',
      'aegis_self_diagnose', 'update_runbook', 'set_service_state',
      'vercel_get_project', 'vercel_list_deployments', 'vercel_get_deployment',
      'vercel_get_build_logs',
      'ledger_query', 'ledger_post', 'ledger_reclassify', 'ledger_add_rule'
  ];

-- 2. Move the schedules that stay from the infra agent to Sebas. The seed
--    carries the same agent_id, and `seed.py` refreshes agent_id on every boot.
UPDATE activities
SET agent_id = 'sebas', updated_at = now()
WHERE slug IN (
        'hub-sweep-5m', 'llm-spend-guard-15min', 'flow-health-watchdog-30m',
        'delivery-watchdog-hourly', 'cleanup-daily', 'agent-task-15min'
    )
  AND agent_id = 'pandoras-actor'
  AND EXISTS (SELECT 1 FROM agents WHERE id = 'sebas');

-- 3. No coding runs from the agent-task sweep: the development lane moved out.
UPDATE activities
SET config = COALESCE(config, '{}'::jsonb) || '{"max_coding": 0}'::jsonb, updated_at = now()
WHERE slug = 'agent-task-15min'
  AND (config->'max_coding') IS DISTINCT FROM '0'::jsonb;

-- 4. Delete the schedules of the lanes that leave. `schedule_sync` prunes the
--    Temporal schedule of a row that is gone on its next pass (~300s).
DELETE FROM activities WHERE slug IN (
    'infra-heartbeat-2m', 'service-drift-4h', 'cert-radar-daily',
    'profile-reflection-weekly-pandoras-actor', 'memory-reflection-nightly-pandoras-actor',
    'sentry-poll-30m', 'jira-sync-30m', 'workspace-repo-sync-daily',
    'money-statements-reconcile', 'receipt-ingest-weekly', 'money-brief-weekly',
    'money-close-monthly'
);

-- 5. Retire the infra agent. Not deleted: other tables point at it. The seed
--    ships it inactive too, because `seed.py` writes `active` on every boot.
UPDATE agents SET active = false, updated_at = now()
WHERE id = 'pandoras-actor' AND active;
