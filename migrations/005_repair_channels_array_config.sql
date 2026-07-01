-- Repair channels rows where config landed as a jsonb ARRAY instead of OBJECT.
--
-- Root cause: core/src/aegis/services/rss_seeder.py passed json.dumps(config)
-- through `$2::jsonb`, which the asyncpg jsonb codec (db/pool.py) then
-- re-encoded — each Miniflux sync wrote EXCLUDED.config as a scalar string.
-- The ON CONFLICT clause `channels.config || EXCLUDED.config` between an
-- object and a scalar string promotes the result to a jsonb array, so
-- repeated startups accumulated arrays like
--   ["{\"label\":...}", "{\"label\":...}"] or
--   [{"label":...}, "{\"label\":...}"].
--
-- This migration unwraps each element (parsing scalar strings back to
-- objects) and merges them into a single object per row.  Later elements
-- win on duplicate keys, which is fine because every element has the same
-- key set in practice.

WITH merged AS (
    SELECT c.id,
           COALESCE(
               (
                   SELECT jsonb_object_agg(kv.key, kv.value)
                   FROM jsonb_array_elements(c.config) AS elem,
                        LATERAL (
                            SELECT CASE
                                WHEN jsonb_typeof(elem) = 'string'
                                    THEN (elem #>> '{}')::jsonb
                                WHEN jsonb_typeof(elem) = 'object'
                                    THEN elem
                                ELSE '{}'::jsonb
                            END AS obj
                        ) AS o,
                        LATERAL jsonb_each(o.obj) AS kv
               ),
               '{}'::jsonb
           ) AS obj
    FROM channels c
    WHERE jsonb_typeof(c.config) = 'array'
)
UPDATE channels c
SET config = merged.obj
FROM merged
WHERE c.id = merged.id;
