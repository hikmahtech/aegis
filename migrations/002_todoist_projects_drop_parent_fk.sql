-- todoist_projects is a denormalized mirror of the Todoist account. Its
-- self-referential parent_id FK assumed the Sync API always returns parents
-- before children, which is false for nested projects — apply_sync_diff then
-- hit a FK violation and the whole sync stalled. The projection doesn't need
-- referential integrity (a dangling parent_id is harmless), so drop the FK.
ALTER TABLE public.todoist_projects
    DROP CONSTRAINT IF EXISTS todoist_projects_parent_id_fkey;
