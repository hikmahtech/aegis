"""FlowHealthWatchdogFlow — the watchdog over AEGIS's own scheduled flows.

Issue #226. AEGIS alerts richly on *infrastructure* (ServiceDrift, CertRadar,
InfraHeartbeat, SentryPoll, DeliveryWatchdog) but had nothing watching its own
flows, so `TodoistSyncFlow` could fail six times in a row (2026-08-02) in total
silence. Shaped after `DeliveryWatchdogFlow`: cheap detect activity, cheap
notify activity, everything deduped so a sustained fault is one alert.

Watches every `workflow_type` in `workflow_runs`, not only scheduled ones —
a child flow (AlertInvestigation, AgentTask) failing every attempt is the same
class of silent breakage and costs nothing extra to catch.

**Who watches the watcher**: nothing does, directly — this flow is itself a
scheduled flow, and if it fails there is no second watchdog. That is bounded
rather than solved, deliberately (a second watchdog just moves the problem):

* it is thin by design — two read-only SELECTs and a fire-and-forget card;
  `safe_send_message` never raises, and both detectors return [] rather than
  throwing on empty/unparseable input, so there is very little that *can* fail;
* its own failures are recorded in `workflow_runs` like everyone else's, and
  the daily briefing's failed-run block plus `/status` already surface those to
  the operator;
* total worker death (the case where no flow runs at all) is covered by the
  existing healthchecks.io dead-man ping in `InfraHeartbeatFlow`;
* it does not exclude itself from detection: after a self-failure the next
  successful run reports its own predecessor like any other flow.
"""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.flow_health import FlowHealthActivities
    from aegis_worker.shared.retry import FAST, NO_RETRY, TIMEOUT_FAST


@dataclass
class FlowHealthConfig:
    agent_id: str = "pandoras-actor"
    # N consecutive failed runs of one workflow_type => alert. 2, not 3: a
    # workflow_runs row is only written once the run's own Temporal retry
    # budget is spent, so the first failure is already a post-retry failure and
    # the second is a wedged flow. 2 also halves detection latency on slow
    # flows (a daily flow alerts on day 2, not day 3).
    consecutive_failures: int = 2
    # Only consider types that ran within this window (see find_failing_flows).
    lookback_hours: int = 24
    # Stale = no SUCCESSFUL run in multiplier x the schedule's own cadence,
    # i.e. roughly two consecutive fires missed plus margin.
    stale_multiplier: float = 3.0
    # Floor under the stale threshold so a 2-min schedule does not alert
    # 6 minutes into a worker redeploy.
    min_stale_minutes: int = 60
    dedup_hours: int = 12
    recovery_hours: int = 168
    check_stale: bool = True
    silent: bool = False  # detect but never notify


@workflow.defn
class FlowHealthWatchdogFlow:
    @workflow.run
    async def run(self, config: FlowHealthConfig) -> dict:
        failing = await workflow.execute_activity_method(
            FlowHealthActivities.find_failing_flows,
            args=[config.consecutive_failures, config.lookback_hours],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=FAST,
        )

        # Best-effort second detector (delivery_watchdog's comms-health
        # pattern): a broken staleness scan must not cost us the consecutive-
        # failure alert, but it is recorded so it cannot rot unnoticed.
        stale: list[dict] = []
        stale_status = "ok"
        if config.check_stale:
            try:
                stale = await workflow.execute_activity_method(
                    FlowHealthActivities.find_stale_flows,
                    args=[config.stale_multiplier, config.min_stale_minutes],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=FAST,
                )
            except Exception:  # noqa: BLE001 — degrade, never take the run down
                stale_status = "check_failed"
        else:
            stale_status = "disabled"

        result = {
            "failing": len(failing),
            "stale": len(stale),
            "stale_status": stale_status,
            "subjects": sorted(f["subject"] for f in [*failing, *stale]),
        }
        if config.silent:
            result["silent"] = True
            return result

        # Called even with zero findings — that is exactly when a previously
        # alerted flow gets its recovery notice.
        report = await workflow.execute_activity_method(
            FlowHealthActivities.report_flow_health,
            args=[
                [*failing, *stale],
                config.agent_id,
                config.dedup_hours,
                config.recovery_hours,
            ],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=NO_RETRY,
        )
        result.update(report)
        return result
