-- 016_clear_poisoned_clarify_watermark.sql
--
-- Backfill for the watermark-invariant fix (PR follow-up to #218/#219).
-- Pre-fix, log_classification bumped todoist_tasks.last_clarified_at
-- regardless of whether apply_outcome actually succeeded. Tasks where the
-- last classification attempt resulted in applied=False AND no follow-up
-- resolution landed are silently abandoned — they never re-enter
-- find_unclassified_items because the watermark is set, but no work was
-- done.
--
-- Clear last_clarified_at on tasks whose most recent gtd_clarify_log row
-- has applied=False AND there is no subsequent row showing recovery
-- (either an applied=True row OR a user-resolution row). Those tasks
-- become re-eligible for classification on the next ClarifyFlow tick.
--
-- This is a one-shot repair; the watermark-invariant change in
-- log_classification + ClarifyFlow ensures new poisoning can't accrue.

WITH latest_log AS (
    SELECT DISTINCT ON (todoist_task_id)
        todoist_task_id,
        applied,
        created_at
    FROM gtd_clarify_log
    ORDER BY todoist_task_id, created_at DESC
),
stuck AS (
    SELECT todoist_task_id
    FROM latest_log
    WHERE applied = false
)
UPDATE todoist_tasks t
SET last_clarified_at = NULL
FROM stuck s
WHERE t.id = s.todoist_task_id
  AND t.is_completed = false
  AND t.last_clarified_at IS NOT NULL;
