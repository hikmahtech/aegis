"""ScheduleHealthFlow - every 4h, check Dagster + n8n schedule health."""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.homelab import HomelabActivities
    from aegis_worker.shared.retry import FAST, NO_RETRY, TIMEOUT_FAST, TIMEOUT_STANDARD


@dataclass
class ScheduleHealthConfig:
    silent: bool = False


@workflow.defn
class ScheduleHealthFlow:
    @workflow.run
    async def run(self, config: ScheduleHealthConfig) -> dict:
        issues_count = 0
        try:
            collected = await workflow.execute_activity_method(
                HomelabActivities.collect_schedules,
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=FAST,
            )
            issues = await workflow.execute_activity_method(
                HomelabActivities.upsert_schedule_health,
                args=[collected],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=FAST,
            )
            issues_count = len(issues)
            if not config.silent:
                for i in issues:
                    try:
                        await workflow.execute_activity_method(
                            HomelabActivities.notify_schedule_issue,
                            args=[i],
                            start_to_close_timeout=TIMEOUT_FAST,
                            retry_policy=NO_RETRY,
                        )
                    except Exception:
                        continue
        except Exception as exc:
            workflow.logger.error("schedule_health_failed error=%s", str(exc)[:200])
            raise
        return {"issues": issues_count}
