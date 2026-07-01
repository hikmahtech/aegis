-- 020_resources_vercel_github_link_index.sql — fast reverse lookup
-- from a github_repo string to its Vercel project resources.
--
-- The Vercel REST API returns a `link` block per project pointing at the
-- backing GitHub repo (when one is connected). VercelProjectSyncFlow stores
-- that in `resources.metadata->>'github_repo'` (string, e.g. "example/drwhome").
--
-- Forward lookup (vercel → github) is free — the value is already on the row.
-- Reverse lookup (given a github_repo, find linked Vercel projects) shows up in:
--   * resolve_alert_resource — when a GitHub alert resolves a repository,
--     surface linked Vercel projects so kimi/the LLM can reason about the
--     deployment that backs the failing code path.
--   * Vercel alerts resolved to a project can find the backing repo cheaply
--     too (but that path is the forward direction — index here is for the
--     reverse pivot).
--
-- We scope the index with a partial predicate on kind='vercel_project' so
-- only the rows that can carry the field are indexed, keeping it small.

CREATE INDEX IF NOT EXISTS resources_vercel_github_repo_idx
  ON resources ((metadata->>'github_repo'))
  WHERE kind = 'vercel_project';
