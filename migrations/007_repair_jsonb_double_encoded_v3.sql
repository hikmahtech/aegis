-- Repair jsonb columns written by worker activities + settings PUT before
-- the second round of double-encoding fixes (drop json.dumps + ::jsonb cast).
-- Same pattern as migrations/004 — values landed as JSON scalar strings
-- (jsonb_typeof = 'string') because Python-side json.dumps + asyncpg's
-- jsonb codec re-encoded the already-JSON string. The expression
-- `col #>> '{}'` unwraps the scalar string to its text, then ::jsonb
-- re-parses to the intended object/array shape.

UPDATE interactions
SET options = (options #>> '{}')::jsonb
WHERE jsonb_typeof(options) = 'string';

UPDATE interactions
SET response = (response #>> '{}')::jsonb
WHERE jsonb_typeof(response) = 'string';

UPDATE audit_log
SET details = (details #>> '{}')::jsonb
WHERE jsonb_typeof(details) = 'string';

UPDATE workflow_runs
SET input_summary = (input_summary #>> '{}')::jsonb
WHERE jsonb_typeof(input_summary) = 'string';

UPDATE workflow_runs
SET result_summary = (result_summary #>> '{}')::jsonb
WHERE jsonb_typeof(result_summary) = 'string';

UPDATE settings
SET value = (value #>> '{}')::jsonb
WHERE key = 'alert_digest_buffer'
  AND jsonb_typeof(value) = 'string';

UPDATE maou.receipt_email
SET parsed = (parsed #>> '{}')::jsonb
WHERE jsonb_typeof(parsed) = 'string';

UPDATE maou.subscription_digest
SET summary = (summary #>> '{}')::jsonb
WHERE jsonb_typeof(summary) = 'string';

UPDATE pandoras_actor.homelab_drift
SET expected = (expected #>> '{}')::jsonb
WHERE jsonb_typeof(expected) = 'string';

UPDATE pandoras_actor.homelab_drift
SET actual = (actual #>> '{}')::jsonb
WHERE jsonb_typeof(actual) = 'string';
