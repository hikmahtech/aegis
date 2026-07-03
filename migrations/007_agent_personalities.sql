-- 007: agent personalities move from image-baked markdown files (and the
-- interim agents.{soul,operating_notes,user_context} columns) into a dedicated
-- table, editable from the admin UI. The personalities/<id>/*.md files become
-- import-on-first-boot starter examples (see aegis.services.personalities).

CREATE TABLE IF NOT EXISTS agent_personalities (
    agent_id   text NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    kind       text NOT NULL CHECK (kind IN ('soul', 'agents', 'user', 'memory')),
    content    text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, kind)
);

-- Backfill from the interim agents persona columns so existing UI edits
-- survive the move (kind mapping: soul->soul, operating_notes->agents,
-- user_context->user).
INSERT INTO agent_personalities (agent_id, kind, content)
SELECT id, 'soul', soul FROM agents WHERE COALESCE(soul, '') <> ''
UNION ALL
SELECT id, 'agents', operating_notes FROM agents WHERE COALESCE(operating_notes, '') <> ''
UNION ALL
SELECT id, 'user', user_context FROM agents WHERE COALESCE(user_context, '') <> ''
ON CONFLICT (agent_id, kind) DO NOTHING;

ALTER TABLE agents
    DROP COLUMN IF EXISTS soul,
    DROP COLUMN IF EXISTS operating_notes,
    DROP COLUMN IF EXISTS user_context;
