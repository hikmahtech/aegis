-- Drop `call_mcp_tool` from every agent's tool set (issue #597).
--
-- The outbound MCP client is gone: no server was ever configured, the
-- subsystem has been dark since 2026-08-29, and the passthrough tool it
-- advertised no longer has an executor. Core warns at startup on any DB
-- `agents.metadata.tool_set` entry naming a missing executor, so a live
-- deployment that still lists it would log that warning on every boot
-- forever.
--
-- Idempotent: an agent whose tool_set never held the name, or already had it
-- removed, is left exactly as it is. A migration is keyed on its FILENAME, so
-- renaming or renumbering this makes it run again — a re-run is a no-op.
--
-- WHICH WAY IT FAILS IN THE WINDOW BEFORE THIS RUNS: the tool is simply not
-- advertised (chat builds the served set from TOOL_EXECUTORS), so an agent
-- still listing it can call nothing extra. The only symptom is the startup
-- warning this removes.

UPDATE agents
SET metadata = jsonb_set(
        metadata,
        '{tool_set}',
        (
            SELECT COALESCE(jsonb_agg(t), '[]'::jsonb)
            FROM jsonb_array_elements(metadata->'tool_set') AS t
            WHERE t <> '"call_mcp_tool"'::jsonb
        )
    )
WHERE jsonb_typeof(metadata->'tool_set') = 'array'
  AND metadata->'tool_set' @> '["call_mcp_tool"]'::jsonb;
