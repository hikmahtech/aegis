"""WorkspaceRepoSyncFlow — workspace scan → reconcile → mirror."""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.inventory import WorkspaceReposInput
    from aegis_worker.flows.workspace_repo_sync import (
        WorkspaceRepoSyncFlow,
        WorkspaceRepoSyncInput,
    )


_SCAN = [
    {
        "path": "personal/aegis",
        "origin_url": "git@github.com:youruser/aegis.git",
        "github_repo": "youruser/aegis",
    },
    {
        "path": "acme/bcp",
        "origin_url": "git@github.com:acme/bcp.git",
        "github_repo": "acme/bcp",
    },
    {
        "path": "infrastructure/infra-gitops",
        "origin_url": "git@github.com:example/infra-gitops.git",
        "github_repo": "example/infra-gitops",
    },
    {
        "path": "personal/example-site",
        "origin_url": "git@github.com:example/example-site.git",
        "github_repo": "example/example-site",
    },
    {
        "path": "trading/trading-system-pipeline",
        "origin_url": "git@github.com:youruser/trading-system-pipeline.git",
        "github_repo": "youruser/trading-system-pipeline",
    },
]

_scan_result: list[dict] = []
_reconcile_calls: list[list[dict]] = []
_mirror_calls: list[list[dict]] = []
_webhook_check_calls: int = 0
_webhook_check_raises: bool = False
_DEFAULT_WEBHOOK_RESULT = {
    "missing_webhooks": ["acme/bcp"],
    "missing_webhooks_count": 1,
    "webhooks_newly_missing": [],
    "webhooks_recovered": [],
    "checked": 4,
    "skipped": 0,
    "webhook_check_status": "ok",
}
_webhook_check_result: dict = dict(_DEFAULT_WEBHOOK_RESULT)


@activity.defn(name="scan_workspace_repos")
async def stub_scan():
    return list(_scan_result)


@activity.defn(name="reconcile_workspace_resources")
async def stub_reconcile(input: WorkspaceReposInput):
    _reconcile_calls.append(list(input.items))
    return {"upserted": len(input.items), "deleted": 2, "deleted_slugs": ["repo-x", "repo-y"]}


@activity.defn(name="mirror_workspace_repos")
async def stub_mirror(input: WorkspaceReposInput):
    _mirror_calls.append(list(input.items))
    return {"present": len(input.items) - 1, "cloned": 1, "cloned_paths": ["personal/example-site"], "failed": []}


@activity.defn(name="check_github_webhooks")
async def stub_check_github_webhooks():
    global _webhook_check_calls
    _webhook_check_calls += 1
    if _webhook_check_raises:
        raise RuntimeError("gh api exploded")
    return dict(_webhook_check_result)


def _reset(scan, *, webhook_result=None, webhook_raises=False):
    global _webhook_check_calls, _webhook_check_result, _webhook_check_raises
    _scan_result.clear()
    _scan_result.extend(scan)
    _reconcile_calls.clear()
    _mirror_calls.clear()
    _webhook_check_calls = 0
    _webhook_check_result = dict(webhook_result or _DEFAULT_WEBHOOK_RESULT)
    _webhook_check_raises = webhook_raises


async def _run_flow(min_repos=5):
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[WorkspaceRepoSyncFlow],
            activities=[stub_scan, stub_reconcile, stub_mirror, stub_check_github_webhooks],
        ),
    ):
        return await env.client.execute_workflow(
            WorkspaceRepoSyncFlow.run,
            WorkspaceRepoSyncInput(agent_id="pandoras-actor", min_repos=min_repos),
            id="ws-sync-1",
            task_queue="tq",
        )


@pytest.mark.asyncio
async def test_workspace_sync_reconciles_and_mirrors_scan():
    _reset(_SCAN)
    result = await _run_flow()
    assert result["status"] == "ok"
    assert result["scanned"] == 5
    assert result["deleted"] == 2
    assert result["mirror"]["cloned"] == 1
    assert result["missing_webhooks"] == ["acme/bcp"]
    assert _reconcile_calls == [_SCAN]
    assert _mirror_calls == [_SCAN]
    assert _webhook_check_calls == 1


@pytest.mark.asyncio
async def test_workspace_sync_aborts_on_suspiciously_small_scan():
    """A scan below min_repos must NOT reconcile (mass-delete guard)."""
    _reset(_SCAN[:2])
    result = await _run_flow(min_repos=5)
    assert result["status"] == "aborted_scan_too_small"
    assert _reconcile_calls == []
    assert _mirror_calls == []
    assert _webhook_check_calls == 0


@pytest.mark.asyncio
async def test_workspace_sync_surfaces_the_webhook_delta_not_just_the_level():
    """#142: the delta keys reach result_summary, where a reader can act on them.

    The standing list is still carried (on-demand readers keep their data) but
    it is `webhooks_newly_missing` that says something changed today.
    """
    _reset(
        _SCAN,
        webhook_result={
            "missing_webhooks": ["acme/bcp", "youruser/aegis"],
            "missing_webhooks_count": 2,
            "webhooks_newly_missing": ["youruser/aegis"],
            "webhooks_recovered": ["example/example-site"],
            "checked": 4,
            "skipped": 0,
            "webhook_check_status": "ok",
        },
    )
    result = await _run_flow()
    assert result["webhooks_newly_missing"] == ["youruser/aegis"]
    assert result["webhooks_recovered"] == ["example/example-site"]
    assert result["missing_webhooks"] == ["acme/bcp", "youruser/aegis"]
    assert result["missing_webhooks_count"] == 2
    assert result["webhook_check_status"] == "ok"


@pytest.mark.asyncio
async def test_workspace_sync_webhook_failure_does_not_poison_the_next_baseline():
    """A raising webhook check degrades to status='failed', never 'ok'.

    The flow must still succeed (the check is best-effort), but the row it
    writes must be excluded from the next run's baseline lookup — an empty
    'ok' set there would make tomorrow re-report the whole backlog as new.
    """
    _reset(_SCAN, webhook_raises=True)
    result = await _run_flow()
    assert result["status"] == "ok"
    assert result["webhook_check_status"] == "failed"
    assert result["missing_webhooks"] == []
    assert result["webhooks_newly_missing"] == []
    assert result["missing_webhooks_count"] == 0
