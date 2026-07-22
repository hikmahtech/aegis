-- Delete the dead VercelProjectSyncFlow schedule (never had a token
-- configured, produced 0 rows ever; deactivated 2026-07-21). Both deletes
-- are 0-impact in prod: the activities row is inactive and resources has 0
-- vercel rows. See aegis#118.
DELETE FROM activities WHERE slug = 'vercel-project-sync-daily';
DELETE FROM resources WHERE kind = 'vercel_project';
