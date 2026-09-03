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
    # Persona revision log (migration 015): one row per programmatic profile
    # patch, forever otherwise. A year keeps the audit trail long enough to
    # answer "when did AEGIS start believing this about me?".
    "agent_profile_revisions": 365,
    # agent_memory_ops_log (migration 020) is DELIBERATELY ABSENT — do not add
    # it without reading this. `prune_old_records` is an unconditional
    # `DELETE … WHERE <ts> < cutoff` with no predicate support, so a retention
    # entry would delete `applied = true` rows too. Those rows are the only
    # record of what an LLM changed in the user's memory, and their
    # `before_content` is the last copy of any row later hard-purged by
    # `purge_retired_memories` — pruning them destroys the reconstruction path
    # the whole A4 design exists to provide. Growth is small and only accrues
    # while `consolidate: true` (a handful of rows per agent per night, capped
    # at _MAX_OPS=200), so unbounded-growth pressure does not apply here.
    # If this ever needs pruning, prune `dry_run = true AND applied = false`
    # rows only — which needs predicate support in prune_old_records first.
    #
    # life.observations (migration 017) — machine-written life metrics
    # (weight, sleep, sensors, pings), unbounded by nature. A year keeps every
    # trend query the chat tool can ask for; pruned by `observed_at`, not
    # created_at (see _TIMESTAMP_COLUMNS). Sibling life.people is deliberately
    # NOT here — that one is user-curated.
    "life.observations": 365,
    # pandoras_actor.* homelab observation tables. Only two remain:
    # backup_health / schedule_health were DROPPED by migration 022, since
    # PR #19 deleted BackupAuditFlow and ScheduleHealthFlow, the only things
    # that ever wrote them (aegis#99). Do not re-add retention for a table
    # that no longer exists.
    "pandoras_actor.homelab_drift": 60,
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
    # Release the git worktree of a coding session whose task is completed or
    # gone and which has been idle this many days. The branch stays — it may
    # back an open PR. Set to 0 to disable.
    task_session_days: int = 7


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

        # Janitor: release the git worktrees of coding sessions whose task is
        # finished. Nothing else on the coding host ever removes them, so
        # skipping this leaves one checkout per @code task there forever.
        # Independent of the sweeps above for the same reason they are of each
        # other — a failure earlier must not silently stop disk being freed.
        if config.task_session_days > 0:
            try:
                session_result = await workflow.execute_activity_method(
                    CleanupActivities.cleanup_task_sessions,
                    args=[config.task_session_days],
                    start_to_close_timeout=TIMEOUT_LONG,
                    retry_policy=NO_RETRY,
                )
                result["task_sessions"] = session_result
            except Exception as exc:
                workflow.logger.error(
                    "task_session_sweep_failed error=%s", str(exc)[:200]
                )
                result["task_sessions"] = {"status": "failed"}

        return result
