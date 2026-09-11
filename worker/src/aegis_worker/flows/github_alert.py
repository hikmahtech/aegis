"""GitHubAlertFlow — webhook-triggered pull-request notifier.

Notifies (Slack, via Pandora) when a pull request is opened / reopened /
marked ready-for-review on a repository the user tracks in `resources` (their
workspace). Scoped to tracked repos so it stays relevant instead of every-repo
noise — which is why it was silenced before.

The `synchronize` action (a new commit pushed to the PR branch) is deliberately
NOT notified: it fires on every push and would spam the user's own active PRs.
Add it to `_NOTIFY_ACTIONS` if you want commit-level updates.

A `closed` PR, merged or not, goes to the problem hub instead (#502,
`HubActivities.follow_fix_pr`). When an investigation opened it from its
Gate-2 card, its problem moves to `verifying` on a merge — the hub sweep then
resolves it once the alert stays clear — or back to `waiting_human` when it
was closed unmerged. Any other PR is nobody's fix, and nothing changes. No
chat message: the problem's task hears it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.homelab import HomelabActivities
    from aegis_worker.activities.hub import HubActivities
    from aegis_worker.shared.retry import FAST, NO_RETRY, TIMEOUT_FAST, TIMEOUT_STANDARD

_NOTIFY_ACTIONS = {"opened", "reopened", "ready_for_review"}
# A run is seconds long, but a deploy can still land mid-run; a closed PR used
# to be filtered with no command at all.
_PATCH_FOLLOW_FIX_PR = "follow-fix-pr-on-close"


@dataclass
class GitHubAlertInput:
    agent_id: str = "pandoras-actor"
    event: str = ""
    delivery_id: str = ""
    payload: dict = field(default_factory=dict)


def _pr_from_payload(payload: dict) -> dict:
    pr = payload.get("pull_request") or {}
    repo = (payload.get("repository") or {}).get("full_name", "")
    return {
        "repo": repo,
        "number": pr.get("number", ""),
        "title": pr.get("title", ""),
        "author": (pr.get("user") or {}).get("login", ""),
        "action": payload.get("action", ""),
        "url": pr.get("html_url", ""),
        "merged": bool(pr.get("merged")),
        "merged_at": pr.get("merged_at") or "",
        "closed_at": pr.get("closed_at") or "",
    }


@workflow.defn(name="GitHubAlertFlow")
class GitHubAlertFlow:
    @workflow.run
    async def run(self, input: GitHubAlertInput) -> dict:
        action = input.payload.get("action")
        if (
            input.event == "pull_request"
            and action == "closed"
            and workflow.patched(_PATCH_FOLLOW_FIX_PR)
        ):
            # FAST: the hub writes are idempotent on GitHub's timestamp, and
            # the webhook claimed the delivery id, so a retry here is the only
            # second chance a merge gets.
            followed = await workflow.execute_activity_method(
                HubActivities.follow_fix_pr,
                args=[_pr_from_payload(input.payload)],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=FAST,
            )
            return {
                "notified": False,
                "reason": "pr_closed",
                "followed": int(followed.get("followed") or 0),
            }
        if input.event != "pull_request" or action not in _NOTIFY_ACTIONS:
            workflow.logger.info(
                "github_pr_skipped event=%s action=%s delivery=%s",
                input.event,
                input.payload.get("action", ""),
                input.delivery_id,
            )
            return {"notified": False, "reason": "filtered"}

        pr = _pr_from_payload(input.payload)
        # notify_pr_event applies the tracked-repo relevance gate + Slack send.
        return await workflow.execute_activity_method(
            HomelabActivities.notify_pr_event,
            args=[pr],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=NO_RETRY,
        )
