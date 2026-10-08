-- 056: the infra lane left v1 (it moved to the v2 DevOps vertical, a2-devops).
-- Its code is gone; this deletes the rows only it read and strips the config
-- that pointed at it.
--
-- Idempotent: a re-run, or a run on a fresh database, changes nothing.
-- Tables are NOT dropped here (`pandoras_actor.*`, `runbooks`,
-- `service_state`); a later PR does that after a dump. The `integration:*`
-- rows of the removed keys (`infra_cluster`, `infra_heartbeat_ping_url`, the
-- homelab and Vercel keys) are kept, as 055 kept its own: boot skips a stored
-- key with no registry entry.

-- Settings rows only the infra lane read or wrote.
DELETE FROM settings WHERE key IN (
    'alert_remediation',      -- the automatic restart's repeat window
    'infra_alert_routing',    -- which alertnames are infra, and their repo
    'hub_settle_seconds',     -- the hub's settle windows (infra sources only)
    'infra_heartbeat_state'   -- InfraHeartbeatFlow's last sample
);

-- The seeded runbook resource. The seed loader would prune it too (runbook is
-- a YAML-owned kind); this does it without waiting for a boot.
DELETE FROM resources WHERE slug = 'homelab-service-restart';

-- Content routes that handed a task to the retired infra agent. Every other
-- route stays; the investigation-only fields a kept route may carry (`gate`,
-- `service`, `resource_tags`, `alert_overrides`) are ignored on read.
UPDATE settings
SET value = COALESCE(
        (SELECT jsonb_agg(r ORDER BY ord)
         FROM jsonb_array_elements(settings.value) WITH ORDINALITY AS e(r, ord)
         WHERE COALESCE(r->>'assignee', '@pandora') <> '@pandora'),
        '[]'::jsonb
    ),
    updated_at = now()
WHERE key = 'content_routes'
  AND jsonb_typeof(value) = 'array'
  AND EXISTS (
      SELECT 1 FROM jsonb_array_elements(value) AS e(r)
      WHERE COALESCE(r->>'assignee', '@pandora') = '@pandora'
  );

-- Agent-task verb overrides naming the retired `infra` verb. The lenient read
-- already ignores them; dropping them keeps the admin page honest.
UPDATE settings
SET value = (
        SELECT COALESCE(jsonb_object_agg(k, v), '{}'::jsonb)
        FROM jsonb_each(settings.value) AS e(k, v)
        WHERE v IS DISTINCT FROM '"infra"'::jsonb
    ),
    updated_at = now()
WHERE key = 'agent_task_verbs'
  AND jsonb_typeof(value) = 'object'
  AND EXISTS (SELECT 1 FROM jsonb_each(value) AS e(k, v) WHERE v = '"infra"'::jsonb);

-- The hub sweep's alertmanager reconcile is gone; drop its config keys.
UPDATE activities
SET config = config - 'alertmanager_url' - 'alertmanager_min_uptime_seconds'
WHERE workflow_type = 'HubSweepFlow'
  AND (config ? 'alertmanager_url' OR config ? 'alertmanager_min_uptime_seconds');
