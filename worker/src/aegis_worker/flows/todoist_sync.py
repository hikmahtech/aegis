"""TodoistSyncFlow — 5-minute incremental sync + outbox drain.

Steps:
1. bootstrap_if_empty — no-op after first successful run.
2. fetch_sync — POST /api/v1/sync with the stored sync_token.
3. apply_sync_diff — upsert projection + save new sync_token.
4. drain_outbox — submit any queued writes.
"""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.delivery import DeliveryActivities
    from aegis_worker.activities.todoist import TodoistActivities
    from aegis_worker.shared.retry import NO_RETRY, TIMEOUT_FAST


@dataclass
class TodoistSyncConfig:
    agent_id: str = "sebas"


@workflow.defn(name="TodoistSyncFlow")
class TodoistSyncFlow:
    @workflow.run
    async def run(self, config: TodoistSyncConfig) -> dict:
        workflow.logger.info("todoist_sync_flow_starting")

        # Track which step failed so workflow_runs.result_summary->>'reason'
        # surfaces e.g. "todoist_sync_failed at step=fetch_sync: ConnectionError(...)"
        # without forcing the operator to open Temporal UI history.
        step = "bootstrap"
        try:
            boot = await workflow.execute_activity_method(
                TodoistActivities.bootstrap_if_empty,
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )

            step = "fetch_sync"
            diff = await workflow.execute_activity_method(
                TodoistActivities.fetch_sync,
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )

            step = "apply_sync_diff"
            apply_result = await workflow.execute_activity_method(
                TodoistActivities.apply_sync_diff,
                args=[diff],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )

            step = "drain_outbox"
            drain_result = await workflow.execute_activity_method(
                TodoistActivities.drain_outbox,
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
        except Exception as exc:  # noqa: BLE001
            raise ApplicationError(
                f"todoist_sync_failed at step={step}: {exc!r}",
                non_retryable=True,
            ) from exc

        # drain_outbox marks a command 'failed' only once (permanent rejection
        # or attempt cap), so failed>0 here means NEW permanent losses this
        # run — alert immediately instead of letting the row rot unseen.
        # Best-effort: a delivery hiccup must not fail the sync flow.
        failed = int(drain_result.get("failed") or 0)
        if failed:
            try:
                await workflow.execute_activity_method(
                    DeliveryActivities.send_message,
                    args=[
                        config.agent_id,
                        (
                            f"🚨 <b>Todoist outbox</b>: {failed} command(s) "
                            "permanently failed this drain — captured work may "
                            "be lost. See /admin/todoist or "
                            "<code>SELECT * FROM todoist_outbox WHERE "
                            "status='failed'</code>."
                        ),
                    ],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            except Exception:  # noqa: BLE001
                workflow.logger.warning("todoist_outbox_failed_alert_undelivered")

        return {
            "bootstrapped": boot.get("bootstrapped", False),
            "sync_token": diff.get("sync_token"),
            "applied": apply_result,
            "drained": drain_result,
        }
