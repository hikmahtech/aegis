"""InventoryActivities — workspace repo sync + GitHub webhook reconciliation
into `resources`."""

from __future__ import annotations

import json

import pytest
from aegis_worker.activities.inventory import (
    InventoryActivities,
    UpsertResourcesBatchInput,
    WorkspaceReposInput,
    _aegis_webhook_present,
    dedupe_workspace_repos,
    origin_to_github_repo,
)
from structlog.testing import capture_logs
from temporalio.testing import ActivityEnvironment


def _make_inv(db_pool=None, *, remote_script=None):
    return InventoryActivities(db_pool=db_pool, remote_script=remote_script)


class FakeRemoteScript:
    """Minimal RemoteScriptConnector stand-in for workspace activities."""

    def __init__(
        self,
        scan=None,
        *,
        scan_host="node-b",
        base_host="node-a",
        present=(),
        hook_responses=None,
    ):
        self._scan = scan or []
        self._scan_host = scan_host
        self._host = base_host
        self._present = set(present)
        self.ensured: list[tuple[str, str]] = []
        # repo -> gh CLI result dict {"status", "stdout", "stderr"}, keyed by
        # the "owner/name" embedded in the run_on_host command string.
        self._hook_responses = hook_responses or {}
        self.hook_calls: list[str] = []

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

    async def run_on_host(self, host, remote_cmd, timeout=30, stdin=None):
        for repo, response in self._hook_responses.items():
            if repo in remote_cmd:
                self.hook_calls.append(repo)
                return response
        return {"status": "failed", "exit_code": -1, "stdout": "", "stderr": "unmocked repo"}


_TEST_SLUG_PREFIX = "test-inv-"


async def _clean_test_resources(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM resources WHERE slug LIKE $1", f"{_TEST_SLUG_PREFIX}%"
        )


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
# _aegis_webhook_present — pure missing-webhook detection logic
# =====================================================================


def test_aegis_webhook_present_true_when_endpoint_in_list():
    hooks = json.dumps(
        ["https://other.example.com/hook", "https://aegis.example.com/api/webhooks/github"]
    )
    assert _aegis_webhook_present(hooks) is True


def test_aegis_webhook_present_false_when_endpoint_absent():
    hooks = json.dumps(["https://other.example.com/hook", "https://ci.example.com/hooks"])
    assert _aegis_webhook_present(hooks) is False


def test_aegis_webhook_present_false_for_empty_hook_list():
    assert _aegis_webhook_present("[]") is False
    assert _aegis_webhook_present("") is False


def test_aegis_webhook_present_none_for_unparseable_output():
    """Garbled/truncated SSH output must be 'skip', never 'missing'."""
    assert _aegis_webhook_present("not json") is None
    assert _aegis_webhook_present('{"not": "a list"}') is None


# =====================================================================
# check_github_webhooks
# =====================================================================


@pytest.mark.asyncio
async def test_check_github_webhooks_no_remote_script_returns_empty():
    inv = _make_inv(remote_script=None)
    env = ActivityEnvironment()
    result = await env.run(inv.check_github_webhooks)
    assert result == {
        "missing_webhooks": [],
        "missing_webhooks_count": 0,
        "webhooks_newly_missing": [],
        "webhooks_recovered": [],
        "webhooks_inconclusive": [],
        "checked": 0,
        "skipped": 0,
        # Not "ok": an unchecked run must never become the delta baseline.
        "webhook_check_status": "skipped",
    }


@pytest.mark.asyncio
async def test_check_github_webhooks_flags_missing_and_skips_unparseable(db_pool):
    await _clean_test_resources(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO resources (kind, slug, title, metadata) VALUES "
            "('repository', $1, 'has hook', $2),"
            "('repository', $3, 'missing hook', $4),"
            "('repository', $5, 'gh 404s', $6)",
            f"{_TEST_SLUG_PREFIX}has-hook",
            {"github_repo": "acme/has-hook"},
            f"{_TEST_SLUG_PREFIX}missing-hook",
            {"github_repo": "acme/missing-hook"},
            f"{_TEST_SLUG_PREFIX}gone",
            {"github_repo": "acme/gone"},
        )
    fake = FakeRemoteScript(
        hook_responses={
            "acme/has-hook": {
                "status": "succeeded",
                "stdout": json.dumps(["https://aegis.example.com/api/webhooks/github"]),
            },
            "acme/missing-hook": {
                "status": "succeeded",
                "stdout": json.dumps(["https://other.example.com/hook"]),
            },
            "acme/gone": {"status": "failed", "stderr": "gh: Not Found (HTTP 404)"},
        }
    )
    inv = _make_inv(db_pool=db_pool, remote_script=fake)
    env = ActivityEnvironment()
    result = await env.run(inv.check_github_webhooks)
    await _clean_test_resources(db_pool)

    assert result["missing_webhooks"] == ["acme/missing-hook"]
    assert result["checked"] == 2
    assert result["skipped"] == 1
    assert set(fake.hook_calls) == {"acme/has-hook", "acme/missing-hook", "acme/gone"}


# =====================================================================
# check_github_webhooks — delta vs the previous run (#142)
# =====================================================================


async def _clean_webhook_runs(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM workflow_runs WHERE workflow_type = 'WorkspaceRepoSyncFlow'"
        )


async def _seed_prior_run(db_pool, missing, *, status="ok", minutes_ago=60):
    """Record a prior WorkspaceRepoSyncFlow run carrying `missing`."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO workflow_runs "
            "(run_id, workflow_id, workflow_type, status, started_at, result_summary) "
            "VALUES ($1, $1, 'WorkspaceRepoSyncFlow', 'completed', "
            "        now() - ($2 || ' minutes')::interval, $3)",
            f"zzmisc-wh-{status}-{minutes_ago}",
            str(minutes_ago),
            {"missing_webhooks": list(missing), "webhook_check_status": status},
        )


async def _seed_repos(db_pool, repos):
    async with db_pool.acquire() as conn:
        for repo in repos:
            await conn.execute(
                "INSERT INTO resources (kind, slug, title, metadata) "
                "VALUES ('repository', $1, $1, $2)",
                f"{_TEST_SLUG_PREFIX}{repo.replace('/', '-')}",
                {"github_repo": repo},
            )


def _hooks(present, absent):
    """gh-CLI stdout map: `present` repos carry AEGIS's hook, `absent` don't."""
    responses = {
        r: {"status": "succeeded", "stdout": json.dumps(["https://aegis.example.com/api/webhooks/github"])}
        for r in present
    }
    responses.update(
        {
            r: {"status": "succeeded", "stdout": json.dumps(["https://other.example.com/hook"])}
            for r in absent
        }
    )
    return responses


@pytest.mark.asyncio
async def test_check_github_webhooks_reports_delta_against_previous_run(db_pool):
    """The change, not the level (#142).

    Previous run recorded {alpha, beta} missing. This run finds {beta, gamma}:
    gamma is news, alpha recovered, beta is the standing set nobody needs told
    about again. Expected values are literals, not re-derived from the code.
    """
    await _clean_test_resources(db_pool)
    await _clean_webhook_runs(db_pool)
    await _seed_prior_run(db_pool, ["acme/alpha", "acme/beta"])
    await _seed_repos(db_pool, ["acme/alpha", "acme/beta", "acme/gamma"])
    fake = FakeRemoteScript(
        hook_responses=_hooks(present=["acme/alpha"], absent=["acme/beta", "acme/gamma"])
    )
    inv = _make_inv(db_pool=db_pool, remote_script=fake)

    with capture_logs() as logs:
        result = await ActivityEnvironment().run(inv.check_github_webhooks)
    await _clean_test_resources(db_pool)
    await _clean_webhook_runs(db_pool)

    assert result["webhooks_newly_missing"] == ["acme/gamma"]
    assert result["webhooks_recovered"] == ["acme/alpha"]
    # Standing set still reported in full — on-demand readers keep their data.
    assert result["missing_webhooks"] == ["acme/beta", "acme/gamma"]
    assert result["missing_webhooks_count"] == 2
    assert result["webhook_check_status"] == "ok"
    # A real change DOES warn, and names only the new repo.
    warnings = [e for e in logs if e["event"] == "github_webhooks_newly_missing"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["repos"] == ["acme/gamma"]


@pytest.mark.asyncio
async def test_check_github_webhooks_standing_set_does_not_warn(db_pool):
    """The 24-day noise case: an unchanged set must produce no warning.

    This is the whole point of #142 — 11-16 repos re-announced every run for
    24 days. Paired with the test above (which proves the warning DOES fire on
    a change), so "no warning" here can't be an artefact of broken logging.
    """
    await _clean_test_resources(db_pool)
    await _clean_webhook_runs(db_pool)
    await _seed_prior_run(db_pool, ["acme/beta", "acme/delta"])
    await _seed_repos(db_pool, ["acme/beta", "acme/delta"])
    fake = FakeRemoteScript(hook_responses=_hooks(present=[], absent=["acme/beta", "acme/delta"]))
    inv = _make_inv(db_pool=db_pool, remote_script=fake)

    with capture_logs() as logs:
        result = await ActivityEnvironment().run(inv.check_github_webhooks)
    await _clean_test_resources(db_pool)
    await _clean_webhook_runs(db_pool)

    assert result["missing_webhooks"] == ["acme/beta", "acme/delta"]
    assert result["missing_webhooks_count"] == 2
    assert result["webhooks_newly_missing"] == []
    assert result["webhooks_recovered"] == []
    assert [e for e in logs if e["log_level"] == "warning"] == []
    # It is still *observed*, just at debug — the signal is quiet, not deleted.
    assert [e["event"] for e in logs if e["event"] == "github_webhooks_missing_unchanged"] == [
        "github_webhooks_missing_unchanged"
    ]


@pytest.mark.asyncio
async def test_check_github_webhooks_inconclusive_repo_is_not_a_recovery(db_pool):
    """A repo we could not check is not a repo that got fixed (#142).

    `acme/beta` was missing last run and `gh` 404s on it this run. Prod does
    this to ~15 of 48 repos every day, and one of them wobbled in and out on
    2026-08-05/06. Its verdict must carry forward, and the delta must stay
    empty in BOTH directions.
    """
    await _clean_test_resources(db_pool)
    await _clean_webhook_runs(db_pool)
    await _seed_prior_run(db_pool, ["acme/beta"])
    await _seed_repos(db_pool, ["acme/alpha", "acme/beta"])
    fake = FakeRemoteScript(
        hook_responses={
            **_hooks(present=["acme/alpha"], absent=[]),
            "acme/beta": {"status": "failed", "stderr": "gh: Not Found (HTTP 404)"},
        }
    )
    inv = _make_inv(db_pool=db_pool, remote_script=fake)

    with capture_logs() as logs:
        result = await ActivityEnvironment().run(inv.check_github_webhooks)
    await _clean_test_resources(db_pool)
    await _clean_webhook_runs(db_pool)

    assert result["webhooks_recovered"] == []
    assert result["webhooks_newly_missing"] == []
    # Carried forward, so tomorrow's baseline still knows beta is missing.
    assert result["missing_webhooks"] == ["acme/beta"]
    assert result["webhooks_inconclusive"] == ["acme/beta"]
    assert result["checked"] == 1
    assert result["skipped"] == 1
    assert [e for e in logs if e["log_level"] == "warning"] == []


@pytest.mark.asyncio
async def test_check_github_webhooks_flake_then_return_does_not_warn(db_pool):
    """The prod false alarm, end to end (#142).

    Two real runs: `acme/beta` is inconclusive in run 1 (recorded as its own
    baseline), then conclusively missing again in run 2. Before the fix run 1
    reported it recovered and run 2 warned it was newly missing — for a
    webhook that never changed. `acme/gamma`, genuinely new, still warns, so
    a silent run 2 can't pass by having broken the warning outright.
    """
    await _clean_test_resources(db_pool)
    await _clean_webhook_runs(db_pool)
    await _seed_prior_run(db_pool, ["acme/beta"], minutes_ago=120)
    await _seed_repos(db_pool, ["acme/beta", "acme/gamma"])

    flaky = FakeRemoteScript(
        hook_responses={
            **_hooks(present=["acme/gamma"], absent=[]),
            "acme/beta": {"status": "failed", "stderr": "gh: Not Found (HTTP 404)"},
        }
    )
    inv = _make_inv(db_pool=db_pool, remote_script=flaky)
    run_one = await ActivityEnvironment().run(inv.check_github_webhooks)
    # Run 1's own output becomes run 2's baseline, exactly as prod records it.
    await _seed_prior_run(db_pool, run_one["missing_webhooks"], minutes_ago=60)

    healthy = FakeRemoteScript(hook_responses=_hooks(present=[], absent=["acme/beta", "acme/gamma"]))
    inv = _make_inv(db_pool=db_pool, remote_script=healthy)
    with capture_logs() as logs:
        run_two = await ActivityEnvironment().run(inv.check_github_webhooks)
    await _clean_test_resources(db_pool)
    await _clean_webhook_runs(db_pool)

    assert run_one["webhooks_recovered"] == []
    assert run_two["webhooks_newly_missing"] == ["acme/gamma"]
    assert run_two["missing_webhooks"] == ["acme/beta", "acme/gamma"]
    warnings = [e for e in logs if e["event"] == "github_webhooks_newly_missing"]
    assert len(warnings) == 1
    assert warnings[0]["repos"] == ["acme/gamma"]


@pytest.mark.asyncio
async def test_check_github_webhooks_first_ever_run_reports_whole_set(db_pool):
    """No baseline at all => the first observation is news, not a standing set."""
    await _clean_test_resources(db_pool)
    await _clean_webhook_runs(db_pool)
    await _seed_repos(db_pool, ["acme/beta", "acme/delta"])
    fake = FakeRemoteScript(hook_responses=_hooks(present=[], absent=["acme/beta", "acme/delta"]))
    inv = _make_inv(db_pool=db_pool, remote_script=fake)

    result = await ActivityEnvironment().run(inv.check_github_webhooks)
    await _clean_test_resources(db_pool)
    await _clean_webhook_runs(db_pool)

    assert result["webhooks_newly_missing"] == ["acme/beta", "acme/delta"]
    assert result["webhooks_recovered"] == []


@pytest.mark.asyncio
async def test_check_github_webhooks_ignores_inconclusive_run_as_baseline(db_pool):
    """An inconclusive run must not reset the baseline to empty.

    A run that bailed (no connector) records `missing_webhooks: []` with
    status='skipped'. If the lookup took the most recent row regardless of
    status, that empty set would become the baseline and the ENTIRE standing
    backlog would re-report as newly missing — reintroducing #142's noise on
    every worker hiccup. The older conclusive row is the baseline instead.
    """
    await _clean_test_resources(db_pool)
    await _clean_webhook_runs(db_pool)
    await _seed_prior_run(db_pool, ["acme/beta", "acme/delta"], status="ok", minutes_ago=120)
    await _seed_prior_run(db_pool, [], status="skipped", minutes_ago=10)
    await _seed_repos(db_pool, ["acme/beta", "acme/delta"])
    fake = FakeRemoteScript(hook_responses=_hooks(present=[], absent=["acme/beta", "acme/delta"]))
    inv = _make_inv(db_pool=db_pool, remote_script=fake)

    result = await ActivityEnvironment().run(inv.check_github_webhooks)
    await _clean_test_resources(db_pool)
    await _clean_webhook_runs(db_pool)

    assert result["missing_webhooks"] == ["acme/beta", "acme/delta"]
    assert result["webhooks_newly_missing"] == []


# =====================================================================
# upsert_resources_batch — exercises real Postgres
# =====================================================================


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
                    "kind": "endpoint",
                    "slug": f"{_TEST_SLUG_PREFIX}new-b",
                    "title": "Inv Test B",
                    "url": "https://b.example.com",
                    "tags": ["other", "test"],
                    "metadata": {"note": "arbitrary second kind for upsert test"},
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
