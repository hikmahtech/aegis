-- The trading desk sells its whole book when the pipeline says it halted
-- (spec docs/superpowers/specs/2026-09-12-maou-trading-desk-design.md §5).
--
-- 'flattened' is the plan outcome for such a day: it names what the desk did
-- with the day, the way 'held_stale' and 'held_suspect' do. `note` keeps the
-- reason ansaar served, so the month close can say why the book was sold.

ALTER TABLE finance.desk_plans DROP CONSTRAINT IF EXISTS desk_plans_outcome_check;

ALTER TABLE finance.desk_plans ADD CONSTRAINT desk_plans_outcome_check
    CHECK (outcome IN ('orders', 'no_change', 'held_stale', 'held_suspect', 'flattened'));

ALTER TABLE finance.desk_plans ADD COLUMN IF NOT EXISTS note text;
