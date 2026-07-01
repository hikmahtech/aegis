-- Personalities as DB config (OSS Phase B). The persona prose that lived in
-- personalities/<id>/{SOUL,AGENTS,USER}.md moves into the agents table so it's
-- edited from the admin UI (and not committed). The .md files become the
-- first-boot seed; the loader reads DB-first and falls back to the files.
-- (MEMORY.md is intentionally not migrated — Phase 4's agent_memory superseded it.)
ALTER TABLE agents ADD COLUMN IF NOT EXISTS soul TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS operating_notes TEXT;  -- was AGENTS.md
ALTER TABLE agents ADD COLUMN IF NOT EXISTS user_context TEXT;     -- was USER.md
