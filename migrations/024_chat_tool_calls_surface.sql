-- chat_tool_calls.surface — WHERE a tool call came from.
--
-- The table was chat-only, so every row was implicitly the chat surface. MCP
-- tool calls (routes/mcp_server.py) were not recorded at all, leaving every
-- coding run's and every operator terminal's tool use unobserved. Recording
-- them into the same table without a marker would be worse than not recording
-- them: a failing tool could no longer be attributed to the surface that ran
-- it, which is exactly the question an operator asks first.
--
-- Values written today:
--   chat          in-process chat loop (services/chat.py) — the default
--   mcp           a run's mount            (POST /api/mcp-server/{agent})
--   mcp_gated     a gated run's mount      (.../gated)
--   mcp_operator  a human's terminal mount (.../operator)
--
-- Deliberately a plain text column with a default rather than an enum: the
-- mount kinds are an application concept that has already changed twice this
-- month, and an enum would need a migration each time.
ALTER TABLE chat_tool_calls
    ADD COLUMN IF NOT EXISTS surface text NOT NULL DEFAULT 'chat';

-- Every "which tools are failing, and where?" query filters on these two.
CREATE INDEX IF NOT EXISTS chat_tool_calls_surface_created_idx
    ON chat_tool_calls (surface, created_at DESC);
