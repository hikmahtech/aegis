-- Problem groups: one problem for the same failure on many entities.
-- Spec: docs/superpowers/specs/2026-09-07-problem-hub-design.md §13.
--
-- Six Postiz posts wedged in the same queue produced six problems and six
-- Todoist tasks. They are one condition, and the fix is one fix. A group
-- problem carries `group_key = '{class}:{subject_kind}'` and absorbs every
-- later occurrence of that class and kind whose own correlation key has no
-- problem, so the seventh stuck post joins the group instead of opening a
-- seventh task.
--
-- A group's own correlation key is '{class}:{subject_kind}:*'. `*` is not a
-- character `hub.py::_slug` can produce, so no real subject can collide with
-- a group's key.

ALTER TABLE problems ADD COLUMN IF NOT EXISTS group_key text;

-- One live group per class + kind. Absorption looks the group up by this key
-- and must never find two.
CREATE UNIQUE INDEX IF NOT EXISTS problems_live_group_key
    ON problems (group_key)
    WHERE closed_at IS NULL AND group_key IS NOT NULL;
