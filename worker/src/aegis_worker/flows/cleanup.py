"""CleanupFlow — nightly retention prune for unbounded ops tables."""

from __future__ import annotations

from dataclasses import dataclass, field

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.cleanup import CleanupActivities
    from aegis_worker.shared.retry import NO_RETRY, TIMEOUT_LONG


_DEFAULT_RETENTIONS: dict[str, int] = {
    "audit_log": 90,
    "llm_calls": 90,
    "connector_calls": 90,
    "chat_tool_calls": 90,
    # 30d chat_history retention: dispatch rows that carry a delivery_ref (or
    # legacy telegram_message_id) get channel-deleted via `cleanup_old_dispatches`
    # BEFORE this generic prune sweeps them; user/assistant rows past 30d
    # are pruned here in DB-only mode.
    "chat_history": 30,
    "governance_decision_log": 90,
    # Operational/dedup tables — bounded growth, useful for short-term debug.
    "workflow_runs": 90,
    "ingest_idempotency": 60,
    "gtd_clarify_log": 180,
    "alert_dedup_index": 60,
    "alert_mutes": 30,
    "pending_prs": 30,
    # inserted unconditionally per webhook (webhooks.py) / per knowledge
    # injection — unbounded growth otherwise (issue #120).
    "todoist_webhook_events": 60,
    "knowledge_injection_log": 90,
    # pandoras_actor.* homelab observation tables — see migration 003.
    "pandoras_actor.homelab_drift": 60,
    "pandoras_actor.backup_health": 60,
    "pandoras_actor.schedule_health": 60,
    "pandoras_actor.cert_expiry": 60,
}


@dataclass
class CleanupConfig:
    retentions: dict[str, int] = field(default_factory=lambda: dict(_DEFAULT_RETENTIONS))
    # Sweep `interactions` rows still pending after this many days, treating
    # them as orphaned by a vanished parent workflow. Set to 0 to disable.
    interaction_orphan_days: int = 7
    # 30-day retention for channel dispatches — rows with delivery_ref (or legacy
    # telegram_message_id) get channel-deleted via the comms adapter before the
    # DB row is dropped. Set to 0 to skip channel cleanup (DB prune still runs).
    dispatch_days: int = 30


@workflow.defn
class CleanupFlow:
    """Runs prune_old_records + archive_orphan_interactions per tick."""

    @workflow.run
    async def run(self, config: CleanupConfig) -> dict:
        workflow.logger.info("cleanup_flow_starting")

        retentions = config.retentions or dict(_DEFAULT_RETENTIONS)

        result: dict = {}

        # Channel-dispatch cleanup runs FIRST: rows with a delivery_ref (or
        # legacy telegram_message_id) are channel-deleted via the comms service
        # before the DB row is dropped — preserving the audit trail on failure.
        # Expired chat_history rows left behind (no ref, or non-dispatch turns)
        # are picked up by prune_old_records below.
        if config.dispatch_days > 0:
            try:
                dispatch_result = await workflow.execute_activity_method(
                    CleanupActivities.cleanup_old_dispatches,
                    args=[config.dispatch_days],
                    start_to_close_timeout=TIMEOUT_LONG,
                    retry_policy=NO_RETRY,
                )
                result["dispatches"] = dispatch_result
            except Exception as exc:
                workflow.logger.error(
                    "dispatch_cleanup_failed error=%s", str(exc)[:200]
                )
                result["dispatches"] = {"status": "failed"}

        try:
            prune_result = await workflow.execute_activity_method(
                CleanupActivities.prune_old_records,
                args=[{"retentions": retentions}],
                start_to_close_timeout=TIMEOUT_LONG,
                retry_policy=NO_RETRY,
            )
            result.update(prune_result)
            total = sum(
                v
                for v in prune_result.values()
                if isinstance(v, int) and v > 0
            )
            workflow.logger.info("cleanup_flow_complete total=%d", total)
        except Exception as exc:
            workflow.logger.error("cleanup_flow_failed error=%s", str(exc)[:200])
            result["prune_status"] = "failed"
            result["prune_error"] = str(exc)[:200]

        # Janitor: sweep orphaned `interactions` rows whose parent workflow
        # vanished before apply_interaction_timeout could fire. Independent
        # of prune_old_records — a prune failure shouldn't suppress the sweep.
        if config.interaction_orphan_days > 0:
            try:
                orphan_result = await workflow.execute_activity_method(
                    CleanupActivities.archive_orphan_interactions,
                    args=[config.interaction_orphan_days],
                    start_to_close_timeout=TIMEOUT_LONG,
                    retry_policy=NO_RETRY,
                )
                result["interactions_archived"] = orphan_result.get("archived", 0)
            except Exception as exc:
                workflow.logger.error(
                    "orphan_interaction_sweep_failed error=%s", str(exc)[:200]
                )
                result["interactions_archived"] = -1

        return result
