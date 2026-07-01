"""WorkspaceRepoSyncFlow — the workspace IS the repository inventory.

Daily sweep that scans the canonical workspace host's `~/Workspace`
hierarchy (node-b in prod) for git checkouts and makes the `resources`
table mirror it exactly: one `kind='repository'` row per checkout with
its workspace-relative `metadata.path`, rows for vanished checkouts
deleted. A repo is a resource iff the owner actually has it checked out —
"repos I work on" — which keeps the alert→resource matcher's candidate
list small and real.

A final mirror step clones any repo missing on the base host (node-a) at
the same relative path, so kimi/claude runs find identical fixed
checkouts on either host (no per-run JIT cloning — that was removed
from `start_kimi_run`).

Safety: an SSH failure raises (never "empty workspace"), and a scan
returning fewer than `min_repos` aborts before the destructive
reconcile — a half-broken scan must not mass-delete the table.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.inventory import WorkspaceReposInput
    from aegis_worker.shared.retry import RETRY_ONCE


_SCAN_TIMEOUT = timedelta(seconds=180)
_RECONCILE_TIMEOUT = timedelta(seconds=60)
# First mirror run clones tens of repos at up to 300s each (sequential,
# heartbeat per repo); steady-state it's one `test -d` per repo.
_MIRROR_TIMEOUT = timedelta(minutes=60)
_MIRROR_HEARTBEAT = timedelta(minutes=10)


@dataclass
class WorkspaceRepoSyncInput:
    agent_id: str = "pandoras-actor"
    min_repos: int = 5  # abort reconcile below this — scan looks broken


@workflow.defn(name="WorkspaceRepoSyncFlow")
class WorkspaceRepoSyncFlow:
    @workflow.run
    async def run(self, input: WorkspaceRepoSyncInput) -> dict:
        repos: list[dict] = await workflow.execute_activity(
            "scan_workspace_repos",
            start_to_close_timeout=_SCAN_TIMEOUT,
            retry_policy=RETRY_ONCE,
        )

        if len(repos) < input.min_repos:
            workflow.logger.warning(
                "workspace_repo_sync_aborted scanned=%d min=%d", len(repos), input.min_repos
            )
            return {"scanned": len(repos), "status": "aborted_scan_too_small"}

        reconcile = await workflow.execute_activity(
            "reconcile_workspace_resources",
            WorkspaceReposInput(items=repos),
            start_to_close_timeout=_RECONCILE_TIMEOUT,
            retry_policy=RETRY_ONCE,
        )

        mirror = await workflow.execute_activity(
            "mirror_workspace_repos",
            WorkspaceReposInput(items=repos),
            start_to_close_timeout=_MIRROR_TIMEOUT,
            heartbeat_timeout=_MIRROR_HEARTBEAT,
            retry_policy=RETRY_ONCE,
        )

        return {"scanned": len(repos), "status": "ok", **reconcile, "mirror": mirror}
