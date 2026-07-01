-- Migration 013: interactions.metadata + source index for Phase 4 clarify spawn
--
-- Phase 4 of the GTD+Todoist integration. See
-- docs/superpowers/specs/2026-05-19-gtd-todoist-phase4-interaction-spawn-design.md.
--
-- ClarifyFlow now spawns InteractionFlow as a fire-and-forget child when
-- it needs the user to disambiguate (low-confidence classifications) or
-- act (2-min in-window). The metadata column carries source, flavor,
-- task_id, and the full classify_one decision so apply_clarify_resolution
-- can dispatch to the right Todoist action after the user picks.

BEGIN;

ALTER TABLE interactions ADD COLUMN metadata JSONB NOT NULL DEFAULT '{}';

-- Partial index keeps prod-check g8 fast: filters pending interactions on
-- metadata->>'source'. We only ever query 'pending' + 'gtd_clarify' here.
CREATE INDEX interactions_metadata_source_idx
  ON interactions ((metadata->>'source'))
  WHERE status='pending';

COMMIT;
