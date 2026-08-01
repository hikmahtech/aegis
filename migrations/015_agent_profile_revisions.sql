-- 015: append-only revision log for the agent persona docs stored in
-- agent_personalities (migration 007). Every programmatic patch written via
-- aegis.services.personalities.apply_profile_patch records the before/after
-- content plus who asked for it, so a bad automated edit can be inspected and
-- reverted. Hand edits through the admin UI (PUT /personality) still go
-- straight to set_personality and are NOT logged here.

CREATE TABLE IF NOT EXISTS agent_profile_revisions (
    id             bigserial PRIMARY KEY,
    agent_id       text NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    kind           text NOT NULL CHECK (kind IN ('soul', 'agents', 'user', 'memory')),
    before_content text,
    after_content  text,
    -- Free-form provenance ('reflection_flow', 'revert', 'chat_tool', …) —
    -- deliberately not an enum so a new writer needs no migration.
    source         text NOT NULL,
    -- The interactions row that authorised the patch, when a human approved
    -- it. No FK: interactions are archived/pruned on their own schedule and a
    -- revision must outlive the decision that produced it.
    interaction_id uuid,
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- "revisions for this agent, newest first" — the only read pattern
-- (list_profile_revisions / GET …/personality/revisions). id DESC breaks ties
-- between rows written inside the same transaction.
CREATE INDEX IF NOT EXISTS idx_agent_profile_revisions_agent_created
    ON agent_profile_revisions (agent_id, created_at DESC, id DESC);
