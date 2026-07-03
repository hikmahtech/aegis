"""InventoryActivities — sync the `resources` table with external sources.

Currently covers:
  * Workspace repositories — git checkouts scanned from the canonical
    workspace host's `~/Workspace` hierarchy (node-b in prod). The scan is
    the single source of truth for `kind='repository'` rows: a repo is a
    resource iff it exists in the workspace ("repos I work on"), with
    `metadata.path` carrying its workspace-relative path (may be nested,
    e.g. "acme/bcp") and `metadata.github_repo` parsed from the
    checkout's origin URL. Reconciliation also DELETES repository rows
    that no longer exist on disk, and a mirror step clones missing repos
    onto the base host (node-a) at the same relative paths so both hosts
    share one hierarchy.
  * Vercel projects (personal + team via REST `/v9/projects`).

`upsert_resources_batch` inserts new rows and union-merges tags /
shallow-merges metadata on existing rows. Title and URL are insert-only
so hand-curated values survive a sync tick.

Vercel projects with `link.type == "github"` carry the linked
`github_repo` ("owner/name") in `metadata.github_repo`, indexed by
migration 020 so a GitHub-side alert can find its Vercel siblings
(see `worker/.../activities/alerts.py::resolve_alert_resource`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
from temporalio import activity

logger = structlog.get_logger()


# =====================================================================
# Activity input dataclasses
# =====================================================================


@dataclass
class WorkspaceReposInput:
    items: list[dict] = field(default_factory=list)  # [{"path", "origin_url"}]


@dataclass
class ListVercelProjectsInput:
    include_personal: bool = True
    team_ids: list[str] = field(default_factory=list)


@dataclass
class UpsertResourcesBatchInput:
    items: list[dict] = field(default_factory=list)


# =====================================================================
# Activity dataclass
# =====================================================================


@dataclass
class InventoryActivities:
    db_pool: Any
    remote_script: Any = None  # RemoteScriptConnector — workspace scan + mirror
    vercel_token: str = ""
    vercel_team_id: str = ""
    http_client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Workspace repositories
    # ------------------------------------------------------------------

    @activity.defn
    async def scan_workspace_repos(self) -> list[dict]:
        """Scan the canonical workspace host for git checkouts.

        Returns deduped [{"path", "origin_url", "github_repo"}] — one entry
        per distinct origin, preferring the categorized path (containing "/")
        when the same repo is checked out at multiple paths (e.g. a stale
        flat JIT clone next to "acme/<name>"). Raises on scan failure
        so the flow never mistakes an unreachable host for an empty workspace.
        """
        if not self.remote_script:
            raise RuntimeError("remote_script connector not configured")
        raw = await self.remote_script.list_workspace_repos()
        return dedupe_workspace_repos(raw)

    @activity.defn
    async def reconcile_workspace_resources(self, input: WorkspaceReposInput) -> dict:
        """Upsert one `kind='repository'` row per scanned repo; delete the rest.

        Upsert mirrors `upsert_resources_batch` semantics (title/url
        insert-only, tags union, metadata shallow-merge — so the scan's
        `path` always overwrites). Rows of kind='repository' whose slug is
        not in this batch are DELETED: the workspace is the source of truth,
        and stale rows are pure noise for the alert→resource matcher.
        """
        items = [_workspace_repo_to_resource_item(r) for r in input.items]
        # Two distinct repos can share a basename across categories; keep the
        # first on the historical `repo-<name>` slug, disambiguate the rest
        # with the full relative path so they don't merge into one row.
        seen: set[str] = set()
        for item in items:
            if item["slug"] in seen:
                item["slug"] = "repo-" + re.sub(r"[^A-Za-z0-9_-]", "-", item["metadata"]["path"])
            seen.add(item["slug"])
        slugs = [i["slug"] for i in items]
        deleted_slugs: list[str] = []
        async with self.db_pool.acquire() as conn, conn.transaction():
            for item in items:
                await conn.execute(
                    """
                    INSERT INTO resources (kind, slug, title, url, tags, metadata, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, NOW())
                    ON CONFLICT (slug) DO UPDATE SET
                      tags = (
                        SELECT COALESCE(array_agg(DISTINCT t), '{}')
                        FROM unnest(resources.tags || EXCLUDED.tags) AS t
                      ),
                      metadata = resources.metadata || EXCLUDED.metadata,
                      updated_at = NOW()
                    """,
                    item["kind"],
                    item["slug"],
                    item.get("title", ""),
                    item.get("url", ""),
                    list(item.get("tags") or []),
                    item.get("metadata") or {},
                )
            rows = await conn.fetch(
                "DELETE FROM resources WHERE kind = 'repository' "
                "AND slug <> ALL($1::text[]) RETURNING slug",
                slugs,
            )
            deleted_slugs = [r["slug"] for r in rows]
        if deleted_slugs:
            logger.info(
                "workspace_resources_pruned",
                count=len(deleted_slugs),
                slugs=deleted_slugs[:20],
            )
        return {
            "upserted": len(items),
            "deleted": len(deleted_slugs),
            "deleted_slugs": deleted_slugs,
        }

    @activity.defn
    async def mirror_workspace_repos(self, input: WorkspaceReposInput) -> dict:
        """Ensure every scanned repo exists on the base host (node-a) at the
        same workspace-relative path, cloning missing ones from origin.

        No-op when the canonical host IS the base host. Heartbeats per repo —
        a first full mirror clones tens of repos.
        """
        if not self.remote_script:
            raise RuntimeError("remote_script connector not configured")
        # DB-first config: refresh before comparing hosts (they may have been
        # edited on the admin Infra page since the last call).
        await self.remote_script.ensure_config()
        if self.remote_script.workspace_scan_host() == self.remote_script._host:
            return {"skipped": "canonical host is base host", "present": 0, "cloned": 0}

        present = 0
        cloned: list[str] = []
        failed: list[dict] = []
        for repo in input.items:
            activity.heartbeat(repo.get("path", ""))
            result = await self.remote_script.ensure_repo(
                repo.get("path", ""), repo.get("origin_url", "")
            )
            if result["status"] == "present":
                present += 1
            elif result["status"] == "cloned":
                cloned.append(repo.get("path", ""))
            else:
                failed.append({"path": repo.get("path", ""), "error": result.get("error", "")})
        if failed:
            logger.warning("workspace_mirror_failures", count=len(failed), failed=failed[:10])
        return {
            "present": present,
            "cloned": len(cloned),
            "cloned_paths": cloned,
            "failed": failed,
        }

    # ------------------------------------------------------------------
    # Vercel
    # ------------------------------------------------------------------

    @activity.defn
    async def list_vercel_projects(
        self, input: ListVercelProjectsInput
    ) -> list[dict]:
        """Return one dict per project across personal + team scopes.

        Dict shape: {id, name, framework, github_repo, production_domain,
        scope, team_id}. `github_repo` is the linked GitHub repo when
        link.type == "github", else "". `production_domain` is the first
        alias under targets.production; "" when none. `scope` is "personal"
        or "team". `team_id` is the resolving team id ("" for personal).
        """
        if not self.vercel_token:
            logger.info("inventory_vercel_no_token_skip")
            return []

        client = self.http_client or httpx.AsyncClient(timeout=30.0)
        owns_client = self.http_client is None
        try:
            out: list[dict] = []
            if input.include_personal:
                out.extend(await self._vercel_list_for_scope(client, team_id=""))
            for team_id in input.team_ids:
                try:
                    out.extend(
                        await self._vercel_list_for_scope(client, team_id=team_id)
                    )
                except httpx.HTTPStatusError as exc:
                    logger.warning(
                        "inventory_vercel_team_failed",
                        team_id=team_id,
                        status=exc.response.status_code,
                    )
                    continue
            return out
        finally:
            if owns_client:
                await client.aclose()

    async def _vercel_list_for_scope(
        self, client: httpx.AsyncClient, *, team_id: str
    ) -> list[dict]:
        scope = "team" if team_id else "personal"
        params: dict[str, Any] = {"limit": 100}
        if team_id:
            params["teamId"] = team_id
        headers = {"Authorization": f"Bearer {self.vercel_token}"}
        out: list[dict] = []
        while True:
            resp = await client.get(
                "https://api.vercel.com/v9/projects",
                params=params,
                headers=headers,
            )
            resp.raise_for_status()
            body = resp.json()
            for proj in body.get("projects", []):
                link = proj.get("link") or {}
                github_repo = ""
                if link.get("type") == "github":
                    org = link.get("org") or link.get("orgName") or ""
                    repo = link.get("repo") or link.get("repoName") or ""
                    if org and repo:
                        github_repo = f"{org}/{repo}"
                production = (proj.get("targets") or {}).get("production") or {}
                aliases = production.get("alias") or []
                production_domain = aliases[0] if aliases else ""
                out.append(
                    {
                        "id": proj.get("id", ""),
                        "name": proj.get("name", ""),
                        "framework": proj.get("framework") or "",
                        "github_repo": github_repo,
                        "production_domain": production_domain,
                        "scope": scope,
                        "team_id": team_id,
                    }
                )
            nxt = (body.get("pagination") or {}).get("next")
            if not nxt:
                break
            params["until"] = nxt
        return out

    # ------------------------------------------------------------------
    # Upsert
    # ------------------------------------------------------------------

    @activity.defn
    async def upsert_resources_batch(
        self, input: UpsertResourcesBatchInput
    ) -> dict:
        """Insert-or-merge each item in a single transaction.

        Update policy (per the design decision):
          * `kind`, `slug` — identity.
          * `title`, `url` — insert-only (never overwritten on update).
            Preserves hand-curated values in config/seed/resources.yaml.
          * `tags` — UNION (array_agg DISTINCT over old || new).
          * `metadata` — JSONB shallow merge (right wins on key collision),
            so right-side keys overwrite but left-side keys outside the new
            set are preserved.

        Returns {inserted, updated, total}.
        """
        if not input.items:
            return {"inserted": 0, "updated": 0, "total": 0}

        inserted = 0
        updated = 0
        async with self.db_pool.acquire() as conn, conn.transaction():
            for item in input.items:
                is_inserted = await conn.fetchval(
                    """
                    INSERT INTO resources (kind, slug, title, url, tags, metadata, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, NOW())
                    ON CONFLICT (slug) DO UPDATE SET
                      tags = (
                        SELECT COALESCE(array_agg(DISTINCT t), '{}')
                        FROM unnest(resources.tags || EXCLUDED.tags) AS t
                      ),
                      metadata = resources.metadata || EXCLUDED.metadata,
                      updated_at = NOW()
                    RETURNING (xmax = 0) AS inserted
                    """,
                    item["kind"],
                    item["slug"],
                    item.get("title", ""),
                    item.get("url", ""),
                    list(item.get("tags") or []),
                    item.get("metadata") or {},
                )
                if is_inserted:
                    inserted += 1
                else:
                    updated += 1
        return {"inserted": inserted, "updated": updated, "total": inserted + updated}


# =====================================================================
# helpers
# =====================================================================


_GITHUB_ORIGIN_RE = re.compile(
    r"^(?:git@github\.com:|https://github\.com/|ssh://git@github\.com/)"
    r"(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$"
)


def origin_to_github_repo(origin_url: str) -> str:
    """Parse "owner/name" out of a GitHub origin URL ("" for non-GitHub)."""
    m = _GITHUB_ORIGIN_RE.match((origin_url or "").strip())
    return m.group("repo") if m else ""


def dedupe_workspace_repos(items: list[dict]) -> list[dict]:
    """One entry per distinct origin, preferring categorized over flat paths.

    A repo checked out at both "<name>" (stale flat JIT clone) and
    "<category>/<name>" collapses to the categorized path. Repos without an
    origin URL are deduped by path. Adds `github_repo` parsed from origin.
    """
    by_key: dict[str, dict] = {}
    for item in sorted(items, key=lambda i: i.get("path", "")):
        path = (item.get("path") or "").strip()
        origin = (item.get("origin_url") or "").strip()
        if not path:
            continue
        key = origin.lower().removesuffix(".git") if origin else f"path:{path}"
        entry = {"path": path, "origin_url": origin, "github_repo": origin_to_github_repo(origin)}
        existing = by_key.get(key)
        if existing is None or ("/" in path and "/" not in existing["path"]):
            by_key[key] = entry
    return sorted(by_key.values(), key=lambda e: e["path"])


def _workspace_repo_to_resource_item(repo: dict) -> dict:
    """Map a scanned workspace repo to a `resources` upsert item.

    Slug keeps the historical `repo-<basename>` convention so existing rows
    (seeded or alert-auto-registered) merge instead of duplicating. Tags
    carry the GitHub org (lowercased — drives `resource_tag_filter` routing,
    e.g. ["acme"] for APP-<n> Jira tickets) and the top-level
    workspace category.
    """
    path = (repo.get("path") or "").strip()
    name = path.rsplit("/", 1)[-1]
    github_repo = repo.get("github_repo") or ""
    tags = ["repository", "workspace", "pandoras-actor"]
    if github_repo:
        tags.append("github")
        org = github_repo.split("/", 1)[0].lower()
        if org and org not in tags:
            tags.append(org)
    if "/" in path:
        category = path.split("/", 1)[0].lower()
        if category not in tags:
            tags.append(category)
    return {
        "kind": "repository",
        "slug": f"repo-{name}",
        "title": github_repo or name,
        "url": f"https://github.com/{github_repo}" if github_repo else "",
        "tags": tags,
        "metadata": {
            "path": path,
            "github_repo": github_repo,
            "origin_url": repo.get("origin_url") or "",
        },
    }
