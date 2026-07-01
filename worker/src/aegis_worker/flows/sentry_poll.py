"""SentryPollFlow — dual-mode (webhook fast-path + 30m poll safety-net).

Webhook: single issue payload → alert → AlertInvestigationFlow child.
Poll: fetch new issues since cursor → dedup → dispatch investigation each →
advance cursor.

Investigations are spawned as ABANDONED children (start_child_workflow +
ParentClosePolicy.ABANDON), never awaited. AlertInvestigationFlow embeds a
human-in-the-loop Gate-2 approval that can stay pending indefinitely; awaiting
it would hold this workflow open, and the poll schedule's OverlapPolicy=Skip
would then skip every subsequent tick — one un-answered approval = total
Sentry blindness. Dispatch-and-advance decouples polling from human latency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.sentry_ingest import (
        FetchNewIssuesInput,
        FetchNewIssuesResult,
    )
    from aegis_worker.flows.alert_investigation import AlertInvestigationFlow
    from aegis_worker.shared.retry import ACT_RETRY


_ACT_TIMEOUT = timedelta(seconds=60)
_FETCH_TIMEOUT = timedelta(seconds=120)


@dataclass
class SentryPollInput:
    agent_id: str = "pandoras-actor"
    mode: str = "poll"  # poll | webhook
    issue: dict = field(default_factory=dict)  # populated when mode='webhook'
    limit: int = 25


@workflow.defn(name="SentryPollFlow")
class SentryPollFlow:
    @workflow.run
    async def run(self, input: SentryPollInput) -> dict:
        if input.mode == "webhook":
            return await self._handle_webhook(input)
        return await self._handle_poll(input)

    async def _handle_webhook(self, input: SentryPollInput) -> dict:
        if not input.issue:
            return {"mode": "webhook", "investigated": 0, "reason": "empty_issue"}

        alert = await workflow.execute_activity(
            "issue_to_alert",
            input.issue,
            start_to_close_timeout=_ACT_TIMEOUT,
            retry_policy=ACT_RETRY,
        )

        new = await workflow.execute_activity(
            "ingest_idempotency_claim",
            args=["sentry", alert["fingerprint"]],
            start_to_close_timeout=_ACT_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        if not new:
            return {"mode": "webhook", "investigated": 0, "reason": "duplicate"}

        # Fire-and-forget: AlertInvestigationFlow contains a human-in-the-loop
        # Gate-2 that can stay pending for hours/days. We must NOT await it or
        # this workflow stays open the whole time. ABANDON keeps the child
        # running after we return. (Same pattern as ClarifyFlow's pandora spawn.)
        await workflow.start_child_workflow(
            AlertInvestigationFlow.run,
            alert,
            id=f"sentry-alert-{alert['fingerprint']}",
            parent_close_policy=workflow.ParentClosePolicy.ABANDON,
        )
        return {"mode": "webhook", "investigated": 1}

    async def _handle_poll(self, input: SentryPollInput) -> dict:
        cursor = await workflow.execute_activity(
            "read_sentry_cursor",
            start_to_close_timeout=_ACT_TIMEOUT,
            retry_policy=ACT_RETRY,
        )

        result: FetchNewIssuesResult = await workflow.execute_activity(
            "fetch_new_issues",
            FetchNewIssuesInput(since_issue_id=cursor, limit=input.limit),
            result_type=FetchNewIssuesResult,
            start_to_close_timeout=_FETCH_TIMEOUT,
            retry_policy=ACT_RETRY,
        )

        investigated = 0
        failed = 0
        for issue in result.issues:
            alert = await workflow.execute_activity(
                "issue_to_alert",
                issue,
                start_to_close_timeout=_ACT_TIMEOUT,
                retry_policy=ACT_RETRY,
            )
            new = await workflow.execute_activity(
                "ingest_idempotency_claim",
                args=["sentry", alert["fingerprint"]],
                start_to_close_timeout=_ACT_TIMEOUT,
                retry_policy=ACT_RETRY,
            )
            if not new:
                continue
            try:
                # Fire-and-forget. The poll's job is to DETECT new issues and
                # DISPATCH an investigation per issue, then advance the cursor —
                # NOT to wait for each investigation (which can block for
                # hours/days on a Gate-2 human approval). Awaiting here meant a
                # single un-answered approval froze the whole poll, and with the
                # schedule's OverlapPolicy=Skip every subsequent 30-min tick was
                # skipped → total Sentry blindness (caught 2026-05-29: 511 skips
                # over 41h). ABANDON keeps each investigation alive after the
                # poll returns. `investigated` now counts dispatched, not
                # resolved, investigations; a failed verdict surfaces on the
                # child's own workflow_runs row, not here.
                await workflow.start_child_workflow(
                    AlertInvestigationFlow.run,
                    alert,
                    id=f"sentry-alert-{alert['fingerprint']}",
                    parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                )
                investigated += 1
            except Exception as exc:
                # Only spawn-time failures land here now (e.g. a child with the
                # same workflow id is already running — benign dedup).
                failed += 1
                workflow.logger.warning(
                    "sentry_investigation_spawn_failed fp=%s err=%s",
                    alert["fingerprint"],
                    str(exc)[:200],
                )

        if result.latest_issue_id:
            await workflow.execute_activity(
                "write_sentry_cursor",
                result.latest_issue_id,
                start_to_close_timeout=_ACT_TIMEOUT,
                retry_policy=ACT_RETRY,
            )

        return {
            "mode": "poll",
            "polled": len(result.issues),
            "investigated": investigated,
            "failed": failed,
        }
