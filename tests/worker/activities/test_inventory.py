"""InventoryActivities — workspace repo + Vercel project sync into `resources`."""

from __future__ import annotations

import json

import pytest
import respx
from aegis_worker.activities.inventory import (
    InventoryActivities,
    ListVercelProjectsInput,
    UpsertResourcesBatchInput,
    WorkspaceReposInput,
    dedupe_workspace_repos,
    origin_to_github_repo,
)
from httpx import Response
from temporalio.testing import ActivityEnvironment


def _make_inv(db_pool=None, *, remote_script=None, vercel_token="vrc_test", team="team_abc"):
    return InventoryActivities(
        db_pool=db_pool,
        remote_script=remote_script,
        vercel_token=vercel_token,
        vercel_team_id=team,
    )


class FakeRemoteScript:
    """Minimal RemoteScriptConnector stand-in for workspace activities."""

    def __init__(self, scan=None, *, scan_host="node-b", base_host="node-a", present=()):
        self._scan = scan or []
        self._scan_host = scan_host
        self._host = base_host
        self._present = set(present)
        self.ensured: list[tuple[str, str]] = []

    def workspace_scan_host(self):
        return self._scan_host

    async def ensure_config(self):
        pass

    async def list_workspace_repos(self, host=""):
        if isinstance(self._scan, Exception):
            raise self._scan
        return list(self._scan)

    async def ensure_repo(self, rel_path, clone_url, host=""):
        self.ensured.append((rel_path, clone_url))
        if rel_path in self._present:
            return {"status": "present", "path": rel_path}
        if not clone_url:
            return {"status": "failed", "path": rel_path, "error": "no origin_url to clone from"}
        return {"status": "cloned", "path": rel_path}


# =====================================================================
# pure helpers
# =====================================================================


def test_origin_to_github_repo_parses_common_forms():
    assert origin_to_github_repo("git@github.com:acme/bcp.git") == "acme/bcp"
    assert origin_to_github_repo("https://github.com/example/example-site") == (
        "example/example-site"
    )
    assert origin_to_github_repo("https://github.com/example/example-site.git") == (
        "example/example-site"
    )
    assert origin_to_github_repo("ssh://git@github.com/org/name.git") == "org/name"
    assert origin_to_github_repo("git@gitlab.com:org/name.git") == ""
    assert origin_to_github_repo("") == ""


def test_dedupe_prefers_categorized_path_over_flat_clone():
    items = [
        {"path": "screener-p-server", "origin_url": "git@github.com:Acme/screener-p-server.git"},
        {
            "path": "acme/screener-p-server",
            "origin_url": "git@github.com:Acme/screener-p-server.git",
        },
        {"path": "acme/bcp", "origin_url": "git@github.com:acme/bcp.git"},
        {"path": "local-only", "origin_url": ""},
    ]
    deduped = dedupe_workspace_repos(items)
    paths = [d["path"] for d in deduped]
    assert "acme/screener-p-server" in paths
    assert "screener-p-server" not in paths
    assert "acme/bcp" in paths
    assert "local-only" in paths
    spy = next(d for d in deduped if d["path"] == "acme/bcp")
    assert spy["github_repo"] == "acme/bcp"


# =====================================================================
# scan_workspace_repos
# =====================================================================


@pytest.mark.asyncio
async def test_scan_workspace_repos_dedupes_and_parses_github():
    fake = FakeRemoteScript(
        scan=[
            {"path": "infra-gitops", "origin_url": "git@github.com:example/infra-gitops.git"},
            {
                "path": "infrastructure/infra-gitops",
                "origin_url": "git@github.com:example/infra-gitops.git",
            },
            {"path": "personal/aegis", "origin_url": "git@github.com:youruser/aegis.git"},
        ]
    )
    inv = _make_inv(remote_script=fake)
    env = ActivityEnvironment()
    repos = await env.run(inv.scan_workspace_repos)
    assert [r["path"] for r in repos] == ["infrastructure/infra-gitops", "personal/aegis"]
    assert repos[1]["github_repo"] == "youruser/aegis"


@pytest.mark.asyncio
async def test_scan_workspace_repos_raises_without_connector():
    inv = _make_inv(remote_script=None)
    env = ActivityEnvironment()
    with pytest.raises(RuntimeError):
        await env.run(inv.scan_workspace_repos)


# =====================================================================
# mirror_workspace_repos
# =====================================================================


@pytest.mark.asyncio
async def test_mirror_skips_when_canonical_is_base():
    fake = FakeRemoteScript(scan_host="node-a", base_host="node-a")
    inv = _make_inv(remote_script=fake)
    env = ActivityEnvironment()
    result = await env.run(
        inv.mirror_workspace_repos,
        WorkspaceReposInput(items=[{"path": "personal/aegis", "origin_url": "x"}]),
    )
    assert result["skipped"]
    assert fake.ensured == []


@pytest.mark.asyncio
async def test_mirror_clones_missing_and_reports_failures():
    fake = FakeRemoteScript(present={"acme/bcp"})
    inv = _make_inv(remote_script=fake)
    env = ActivityEnvironment()
    result = await env.run(
        inv.mirror_workspace_repos,
        WorkspaceReposInput(
            items=[
                {"path": "acme/bcp", "origin_url": "git@github.com:acme/bcp.git"},
                {"path": "personal/aegis", "origin_url": "git@github.com:youruser/aegis.git"},
                {"path": "local-only", "origin_url": ""},
            ]
        ),
    )
    assert result["present"] == 1
    assert result["cloned"] == 1
    assert result["cloned_paths"] == ["personal/aegis"]
    assert result["failed"] == [{"path": "local-only", "error": "no origin_url to clone from"}]


# =====================================================================
# list_vercel_projects
# =====================================================================


@pytest.mark.asyncio
@respx.mock
async def test_list_vercel_projects_personal_extracts_github_link():
    inv = _make_inv(team="")
    respx.get("https://api.vercel.com/v9/projects").mock(
        return_value=Response(
            200,
            json={
                "projects": [
                    {
                        "id": "prj_abc",
                        "name": "example-site",
                        "framework": "nextjs",
                        "link": {
                            "type": "github",
                            "org": "example",
                            "repo": "example-site",
                            "productionBranch": "main",
                        },
                        "targets": {
                            "production": {"alias": ["example.com", "example-site.vercel.app"]}
                        },
                    },
                    {
                        "id": "prj_def",
                        "name": "acme-marketing",
                        "framework": "astro",
                        "link": {"type": "gitlab", "projectId": "12345"},
                        "targets": {"production": {"alias": ["acme-marketing.vercel.app"]}},
                    },
                    {
                        "id": "prj_ghi",
                        "name": "no-git-project",
                        "framework": None,
                        "link": None,
                        "targets": {"production": {"alias": ["no-git.vercel.app"]}},
                    },
                ],
                "pagination": {"next": None},
            },
        )
    )
    env = ActivityEnvironment()
    projects = await env.run(
        inv.list_vercel_projects,
        ListVercelProjectsInput(include_personal=True, team_ids=[]),
    )
    assert len(projects) == 3
    example_site = next(p for p in projects if p["name"] == "example-site")
    assert example_site["github_repo"] == "example/example-site"
    assert example_site["production_domain"] == "example.com"
    assert example_site["scope"] == "personal"
    acme = next(p for p in projects if p["name"] == "acme-marketing")
    assert acme["github_repo"] == ""
    no_git = next(p for p in projects if p["name"] == "no-git-project")
    assert no_git["github_repo"] == ""


@pytest.mark.asyncio
@respx.mock
async def test_list_vercel_projects_team_scope_passes_team_id():
    inv = _make_inv()
    route = respx.get("https://api.vercel.com/v9/projects").mock(
        return_value=Response(200, json={"projects": [], "pagination": {"next": None}})
    )
    env = ActivityEnvironment()
    await env.run(
        inv.list_vercel_projects,
        ListVercelProjectsInput(include_personal=False, team_ids=["team_abc"]),
    )
    assert route.call_count == 1
    request = route.calls[0].request
    assert "teamId=team_abc" in str(request.url)


@pytest.mark.asyncio
@respx.mock
async def test_list_vercel_projects_personal_plus_team_two_passes():
    inv = _make_inv()
    route = respx.get("https://api.vercel.com/v9/projects").mock(
        side_effect=[
            Response(
                200,
                json={
                    "projects": [
                        {
                            "id": "prj_p",
                            "name": "personal-proj",
                            "framework": None,
                            "link": None,
                            "targets": {"production": None},
                        }
                    ],
                    "pagination": {"next": None},
                },
            ),
            Response(
                200,
                json={
                    "projects": [
                        {
                            "id": "prj_t",
                            "name": "team-proj",
                            "framework": None,
                            "link": None,
                            "targets": {"production": None},
                        }
                    ],
                    "pagination": {"next": None},
                },
            ),
        ]
    )
    env = ActivityEnvironment()
    projects = await env.run(
        inv.list_vercel_projects,
        ListVercelProjectsInput(include_personal=True, team_ids=["team_abc"]),
    )
    assert route.call_count == 2
    names = {p["name"] for p in projects}
    assert names == {"personal-proj", "team-proj"}


@pytest.mark.asyncio
@respx.mock
async def test_list_vercel_projects_no_token_returns_empty():
    inv = _make_inv(vercel_token="")
    env = ActivityEnvironment()
    projects = await env.run(
        inv.list_vercel_projects,
        ListVercelProjectsInput(include_personal=True, team_ids=["team_abc"]),
    )
    assert projects == []


# =====================================================================
# upsert_resources_batch — exercises real Postgres
# =====================================================================


_TEST_SLUG_PREFIX = "test-inv-"


async def _clean_test_resources(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM resources WHERE slug LIKE $1", f"{_TEST_SLUG_PREFIX}%"
        )


@pytest.mark.asyncio
async def test_upsert_resources_batch_inserts_new_rows(db_pool):
    await _clean_test_resources(db_pool)
    inv = _make_inv(db_pool=db_pool)
    env = ActivityEnvironment()
    summary = await env.run(
        inv.upsert_resources_batch,
        UpsertResourcesBatchInput(
            items=[
                {
                    "kind": "repository",
                    "slug": f"{_TEST_SLUG_PREFIX}new-a",
                    "title": "Inv Test A",
                    "url": "https://example.com/a",
                    "tags": ["github", "test"],
                    "metadata": {"path": "new-a", "github_repo": "org/new-a"},
                },
                {
                    "kind": "vercel_project",
                    "slug": f"{_TEST_SLUG_PREFIX}new-b",
                    "title": "Inv Test B",
                    "url": "https://b.example.com",
                    "tags": ["vercel", "test"],
                    "metadata": {"project_id": "prj_b", "github_repo": "org/b"},
                },
            ]
        ),
    )
    assert summary == {"inserted": 2, "updated": 0, "total": 2}
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT slug, kind, title, tags, metadata::text FROM resources "
            "WHERE slug LIKE $1 ORDER BY slug",
            f"{_TEST_SLUG_PREFIX}%",
        )
    assert len(rows) == 2
    a = rows[0]
    assert a["slug"] == f"{_TEST_SLUG_PREFIX}new-a"
    assert a["kind"] == "repository"
    assert a["title"] == "Inv Test A"
    assert set(a["tags"]) == {"github", "test"}
    meta_a = json.loads(a["metadata"])
    assert meta_a == {"path": "new-a", "github_repo": "org/new-a"}
    await _clean_test_resources(db_pool)


@pytest.mark.asyncio
async def test_upsert_resources_batch_tag_union_metadata_merge(db_pool):
    """Existing row should get tags UNIONED, metadata SHALLOW-MERGED (right wins).
    Title and URL are insert-only — must NOT change on conflict."""
    await _clean_test_resources(db_pool)
    slug = f"{_TEST_SLUG_PREFIX}existing"
    async with db_pool.acquire() as conn:
        # NOTE: the asyncpg pool has a JSONB codec installed
        # (core/src/aegis/db/pool.py::_init_connection) that auto-encodes
        # dicts via json.dumps. Pre-stringifying double-encodes and stores
        # a JSONB string primitive rather than an object.
        await conn.execute(
            "INSERT INTO resources (kind, slug, title, url, tags, metadata) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            "repository",
            slug,
            "Hand-curated Title",
            "https://hand.example.com",
            ["hand-tag-1", "hand-tag-2"],
            {
                "path": "existing",
                "description": "old desc",
                "hand_added": "keep this",
            },
        )

    inv = _make_inv(db_pool=db_pool)
    env = ActivityEnvironment()
    summary = await env.run(
        inv.upsert_resources_batch,
        UpsertResourcesBatchInput(
            items=[
                {
                    "kind": "repository",
                    "slug": slug,
                    "title": "Auto-Generated Title",
                    "url": "https://api.example.com/new",
                    "tags": ["hand-tag-2", "github", "auto-added"],
                    "metadata": {
                        "description": "new desc from API",
                        "default_branch": "main",
                        "archived": False,
                    },
                }
            ]
        ),
    )
    assert summary == {"inserted": 0, "updated": 1, "total": 1}
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT title, url, tags, metadata::text FROM resources WHERE slug=$1", slug
        )
    assert row["title"] == "Hand-curated Title"
    assert row["url"] == "https://hand.example.com"
    assert set(row["tags"]) == {"hand-tag-1", "hand-tag-2", "github", "auto-added"}
    meta = json.loads(row["metadata"])
    assert meta["description"] == "new desc from API"
    assert meta["default_branch"] == "main"
    assert meta["archived"] is False
    assert meta["hand_added"] == "keep this"
    assert meta["path"] == "existing"
    await _clean_test_resources(db_pool)


@pytest.mark.asyncio
async def test_upsert_resources_batch_empty_returns_zero(db_pool):
    inv = _make_inv(db_pool=db_pool)
    env = ActivityEnvironment()
    summary = await env.run(
        inv.upsert_resources_batch, UpsertResourcesBatchInput(items=[])
    )
    assert summary == {"inserted": 0, "updated": 0, "total": 0}


# =====================================================================
# reconcile_workspace_resources — exercises real Postgres
# =====================================================================


@pytest.mark.asyncio
async def test_reconcile_upserts_scanned_and_deletes_stale_repos(db_pool):
    """The workspace scan is the source of truth for kind='repository':
    scanned repos are upserted with their nested path, repository rows not
    in the scan are deleted, and non-repository kinds are untouched."""
    await _clean_test_resources(db_pool)
    async with db_pool.acquire() as conn:
        # The local test DB may carry these from earlier seed runs.
        await conn.execute("DELETE FROM resources WHERE slug IN ('repo-bcp', 'repo-aegis')")
        # Stale repo row (org-sync pollution) — must be deleted.
        await conn.execute(
            "INSERT INTO resources (kind, slug, title, tags, metadata) "
            "VALUES ('repository', $1, 'Stale Org Repo', '{}', $2)",
            f"{_TEST_SLUG_PREFIX}stale-org-repo",
            {"path": "stale-org-repo", "github_repo": "Acme/stale-org-repo"},
        )
        # Existing repo row with hand-curated title + old FLAT path — title
        # survives (insert-only), path is overwritten by the scan.
        await conn.execute(
            "INSERT INTO resources (kind, slug, title, tags, metadata) "
            "VALUES ('repository', $1, 'Acme BCP — data ingest', $2, $3)",
            "repo-bcp",
            ["acme", "pandoras-actor"],
            {"path": "bcp", "github_repo": "acme/bcp"},
        )
        # Non-repository kind — must never be touched by the reconcile.
        await conn.execute(
            "INSERT INTO resources (kind, slug, title, tags, metadata) "
            "VALUES ('connector', $1, 'Test Connector', '{}', '{}') "
            "ON CONFLICT (slug) DO NOTHING",
            f"{_TEST_SLUG_PREFIX}connector",
        )

    inv = _make_inv(db_pool=db_pool)
    env = ActivityEnvironment()
    result = await env.run(
        inv.reconcile_workspace_resources,
        WorkspaceReposInput(
            items=[
                {
                    "path": "acme/bcp",
                    "origin_url": "git@github.com:acme/bcp.git",
                    "github_repo": "acme/bcp",
                },
                {
                    "path": "personal/aegis",
                    "origin_url": "git@github.com:youruser/aegis.git",
                    "github_repo": "youruser/aegis",
                },
            ]
        ),
    )
    assert result["upserted"] == 2
    assert f"{_TEST_SLUG_PREFIX}stale-org-repo" in result["deleted_slugs"]

    async with db_pool.acquire() as conn:
        bcp = await conn.fetchrow(
            "SELECT title, tags, metadata::text FROM resources WHERE slug = 'repo-bcp'"
        )
        aegis_row = await conn.fetchrow(
            "SELECT tags, metadata::text FROM resources WHERE slug = 'repo-aegis'"
        )
        stale = await conn.fetchval(
            "SELECT 1 FROM resources WHERE slug = $1", f"{_TEST_SLUG_PREFIX}stale-org-repo"
        )
        connector = await conn.fetchval(
            "SELECT 1 FROM resources WHERE slug = $1", f"{_TEST_SLUG_PREFIX}connector"
        )
        # cleanup rows this test owns
        await conn.execute("DELETE FROM resources WHERE slug IN ('repo-bcp', 'repo-aegis')")
    await _clean_test_resources(db_pool)

    assert stale is None
    assert connector == 1
    assert bcp["title"] == "Acme BCP — data ingest"  # insert-only title kept
    assert json.loads(bcp["metadata"])["path"] == "acme/bcp"  # path overwritten
    assert "acme" in bcp["tags"]
    meta = json.loads(aegis_row["metadata"])
    assert meta["path"] == "personal/aegis"
    assert meta["github_repo"] == "youruser/aegis"
    assert {"repository", "workspace", "pandoras-actor", "github", "youruser", "personal"} <= set(
        aegis_row["tags"]
    )


@pytest.mark.asyncio
async def test_reconcile_disambiguates_basename_slug_collisions(db_pool):
    """Two distinct repos sharing a basename must not merge into one row."""
    await _clean_test_resources(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM resources WHERE slug IN ('repo-tool', 'repo-swarm-tool')"
        )
    inv = _make_inv(db_pool=db_pool)
    env = ActivityEnvironment()
    result = await env.run(
        inv.reconcile_workspace_resources,
        WorkspaceReposInput(
            items=[
                {"path": "personal/tool", "origin_url": "git@github.com:a/tool.git",
                 "github_repo": "a/tool"},
                {"path": "swarm/tool", "origin_url": "git@github.com:b/tool.git",
                 "github_repo": "b/tool"},
            ]
        ),
    )
    assert result["upserted"] == 2
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT slug FROM resources WHERE slug IN ('repo-tool', 'repo-swarm-tool') "
            "ORDER BY slug"
        )
        await conn.execute(
            "DELETE FROM resources WHERE slug IN ('repo-tool', 'repo-swarm-tool')"
        )
    assert [r["slug"] for r in rows] == ["repo-swarm-tool", "repo-tool"]
