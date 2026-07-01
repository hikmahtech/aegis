-- Repair rows written by seed.py before the json.dumps double-encoding fix.
-- Affected columns were seeded with `$N::jsonb` + a Python-side json.dumps AND
-- an asyncpg jsonb codec (db/pool.py) — the codec re-encoded the already-JSON
-- string, so values landed as JSON scalar strings (jsonb_typeof = 'string')
-- instead of objects/arrays. `config #>> '{}'` unwraps the scalar string to its
-- text, then ::jsonb re-parses to the intended shape.

UPDATE channels
SET config = (config #>> '{}')::jsonb
WHERE jsonb_typeof(config) = 'string';

UPDATE agents
SET capabilities = (capabilities #>> '{}')::jsonb
WHERE jsonb_typeof(capabilities) = 'string';

UPDATE resources
SET metadata = (metadata #>> '{}')::jsonb
WHERE jsonb_typeof(metadata) = 'string';

UPDATE activities
SET config = (config #>> '{}')::jsonb
WHERE jsonb_typeof(config) = 'string';
