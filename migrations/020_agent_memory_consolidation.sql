-- 020: safety rails for LLM-driven memory consolidation (A4).
--
-- agent_memory rows are the user's own accumulated knowledge — mostly
-- human-authored corrections. The A3 planner proposes ADD/UPDATE/DELETE ops
-- over them with an LLM; A4 lets those ops actually apply. Everything here
-- exists so that a wrong plan is RECOVERABLE, not merely unlikely:
--
--   * superseded_at / superseded_by turn "DELETE" into a soft retire. The row
--     stays SELECT-able forever; only the read helpers filter it out. Undo is
--     `UPDATE agent_memory SET superseded_at = NULL, superseded_by = NULL`.
--   * agent_memory_ops_log records every op the planner PROPOSED — in dry-run
--     as well as apply mode — with the before/after content, so the prior
--     state of a mutated row can be reconstructed from the log alone.
--
-- Idempotent: re-running is a no-op.

ALTER TABLE agent_memory
    -- NULL = live. Non-NULL = soft-retired: excluded from every read helper
    -- (recent_memories / all_memories) but still present and restorable.
    --
    -- NOTE the retirement marker is superseded_AT, not superseded_BY. A DELETE
    -- op does not necessarily name a successor row, so superseded_by would be
    -- NULL for most retirements and could not serve as the "is retired"
    -- predicate. superseded_by is the OPTIONAL provenance link (the row that
    -- absorbed this one, when the plan said so).
    ADD COLUMN IF NOT EXISTS superseded_at        timestamptz,
    ADD COLUMN IF NOT EXISTS superseded_by        bigint,
    -- Last time this row was touched by a consolidation pass (UPDATE or
    -- retire). Lets an operator see which rows the LLM has been rewriting.
    ADD COLUMN IF NOT EXISTS last_consolidated_at timestamptz;

-- Every read path filters on superseded_at IS NULL; the existing
-- agent_memory_agent_idx no longer fully covers them.
CREATE INDEX IF NOT EXISTS agent_memory_live_idx
    ON agent_memory (agent_id, importance DESC, created_at DESC)
    WHERE superseded_at IS NULL;

-- Append-only ledger of consolidation operations.
--
-- One row per op the planner PROPOSED, whether or not it was applied:
--   * dry_run = true             — observed only, nothing was written.
--   * applied = false + skip_reason — vetoed by a safety rail (protected row,
--                                     quota refusal, kill switch).
--   * applied = true             — the mutation actually happened; before_*
--                                  columns are the pre-image.
--
-- Deliberately NOT pruned by CleanupFlow: this is the record of what was
-- deleted. See the comment in worker/src/aegis_worker/flows/cleanup.py.
CREATE TABLE IF NOT EXISTS agent_memory_ops_log (
    id               bigserial PRIMARY KEY,
    agent_id         text NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    -- Temporal workflow run that proposed the op. No FK — workflow_runs is
    -- pruned at 90 days and this ledger must outlive it.
    run_id           text,
    op               text NOT NULL CHECK (op IN ('ADD', 'UPDATE', 'DELETE', 'NOOP')),
    -- NULL for ADD/NOOP (no pre-existing row). No FK: the log must survive a
    -- later hard purge of the row it describes — that is the whole point.
    memory_id        bigint,
    before_content   text,
    after_content    text,
    before_importance real,
    after_importance  real,
    dry_run          boolean NOT NULL,
    applied          boolean NOT NULL DEFAULT false,
    -- Why an op was not applied: 'quota_exceeded', 'protected_recent',
    -- 'protected_importance', 'protected_dedupe_marker', 'apply_disabled', …
    skip_reason      text,
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- "what did consolidation do to this agent, newest first" — the only read
-- pattern (GET /api/admin/agents/{id}/memory/ops). id DESC breaks ties between
-- rows written inside the same transaction.
CREATE INDEX IF NOT EXISTS idx_agent_memory_ops_log_agent_created
    ON agent_memory_ops_log (agent_id, created_at DESC, id DESC);
