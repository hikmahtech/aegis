-- 015_pandora_alert_link.sql — index for Pandora's active-task lookup.
--
-- ClarifyFlow short-circuits when a task already carries the @pandora
-- label (AlertInvestigation has claimed it). A GIN index over labels on
-- still-open tasks makes that membership test O(1).

CREATE INDEX IF NOT EXISTS idx_todoist_tasks_labels_gin_open
  ON todoist_tasks USING gin (labels)
  WHERE NOT is_completed;
