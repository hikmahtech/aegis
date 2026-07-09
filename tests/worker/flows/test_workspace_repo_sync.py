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


def _reset(scan):
    _scan_result.clear()
    _scan_result.extend(scan)
    _reconcile_calls.clear()
    _mirror_calls.clear()


async def _run_flow(min_repos=5):
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[WorkspaceRepoSyncFlow],
            activities=[stub_scan, stub_reconcile, stub_mirror],
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
    assert _reconcile_calls == [_SCAN]
    assert _mirror_calls == [_SCAN]


@pytest.mark.asyncio
async def test_workspace_sync_aborts_on_suspiciously_small_scan():
    """A scan below min_repos must NOT reconcile (mass-delete guard)."""
    _reset(_SCAN[:2])
    result = await _run_flow(min_repos=5)
    assert result["status"] == "aborted_scan_too_small"
    assert _reconcile_calls == []
    assert _mirror_calls == []
