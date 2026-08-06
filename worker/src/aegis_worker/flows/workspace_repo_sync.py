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

A final, best-effort step checks that each tracked repo still has AEGIS's
GitHub webhook registered (`check_github_webhooks`, detection only — see
aegis#118) and folds the result into this flow's result_summary. A failure
here never fails the flow: the reconcile/mirror steps above are the
load-bearing part of this daily run.

**That step reports a change, not a level (aegis#142).** It used to warn on
the standing `missing_webhooks` set every single day — 11-16 of 33 repos for
24 days running, a number that by construction never reaches zero because
most tracked checkouts are client repos that should *not* have a webhook
pointing at a homelab endpoint. The alternative, auto-creating the hooks,
was rejected: it is an outward-facing mutation against third-party repos,
and the check has no way to tell "should have a webhook and doesn't" from
"was never meant to have one", so it would mass-create ~14 unwanted hooks.
Instead the warning fires only on `webhooks_newly_missing` — a webhook
that vanished, or a newly-tracked repo — while the full standing list stays
in `result_summary.missing_webhooks` and this flow is chat-triggerable, so
the report is still reachable on demand rather than dropped.

The delta then had to be made flake-proof to be worth anything. ~15 of 48
tracked repos are permanently inconclusive (the token isn't an admin there,
so `gh api .../hooks` 404s), and membership of that group wobbles run to
run. Because an inconclusive repo simply fell out of `missing_webhooks`, it
was reported as `webhooks_recovered` and then, on its return, warned about
as `webhooks_newly_missing` — prod did exactly this to
`arshadansari27/stranger-to-sold-site` on 2026-08-05/06 for a webhook that
never changed. An inconclusive repo now keeps its previous verdict and stays
out of the delta.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.inventory import WorkspaceReposInput
    from aegis_worker.shared.retry import NO_RETRY, RETRY_ONCE


_SCAN_TIMEOUT = timedelta(seconds=180)
_RECONCILE_TIMEOUT = timedelta(seconds=60)
# First mirror run clones tens of repos at up to 300s each (sequential,
# heartbeat per repo); steady-state it's one `test -d` per repo.
_MIRROR_TIMEOUT = timedelta(minutes=60)
_MIRROR_HEARTBEAT = timedelta(minutes=10)
# One `gh api` SSH round-trip per tracked repo (sequential, heartbeat per repo).
_WEBHOOK_CHECK_TIMEOUT = timedelta(minutes=10)
_WEBHOOK_CHECK_HEARTBEAT = timedelta(minutes=2)


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

        # Detection-only webhook reconciliation — never fails the flow.
        try:
            webhook_check = await workflow.execute_activity(
                "check_github_webhooks",
                start_to_close_timeout=_WEBHOOK_CHECK_TIMEOUT,
                heartbeat_timeout=_WEBHOOK_CHECK_HEARTBEAT,
                retry_policy=NO_RETRY,
            )
            # Only the DELTA warns (#142) — the standing set is carried in the
            # result_summary for on-demand reads, not re-announced every day.
            if webhook_check.get("webhooks_newly_missing"):
                workflow.logger.warning(
                    "workspace_repo_sync_webhooks_newly_missing repos=%s standing=%s",
                    webhook_check["webhooks_newly_missing"],
                    webhook_check.get("missing_webhooks_count"),
                )
        except Exception as exc:
            workflow.logger.error("github_webhook_check_failed error=%s", str(exc)[:200])
            # status='failed' keeps this empty set from becoming the next run's
            # baseline, which would flag the whole backlog as newly missing.
            webhook_check = {
                "missing_webhooks": [],
                "missing_webhooks_count": 0,
                "webhooks_newly_missing": [],
                "webhooks_recovered": [],
                "webhooks_inconclusive": [],
                "webhook_check_status": "failed",
            }

        return {
            "scanned": len(repos),
            "status": "ok",
            **reconcile,
            "mirror": mirror,
            **webhook_check,
        }
