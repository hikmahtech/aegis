"""SocialMetricsFlow — pull Postiz analytics into social_outbox.metrics.

Daily: one activity call, refresh_post_metrics, which walks recently-posted
Postiz-routed social_outbox rows and caches each post's latest analytics
series (likes/comments/etc.) + Postiz state/release_url/publish_date onto
the row. No gate on `social_publishing_enabled` — metrics on already-posted
rows are harmless to refresh even while publishing itself is switched off.
"""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.social import SocialActivities
    from aegis_worker.shared.retry import NO_RETRY, TIMEOUT_STANDARD


@dataclass
class SocialMetricsConfig:
    agent_id: str
    window_days: int = 14


@workflow.defn(name="SocialMetricsFlow")
class SocialMetricsFlow:
    @workflow.run
    async def run(self, config: SocialMetricsConfig) -> dict:
        step = "refresh_post_metrics"
        try:
            result = await workflow.execute_activity_method(
                SocialActivities.refresh_post_metrics,
                args=[config.window_days],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=NO_RETRY,
            )
        except Exception as exc:  # noqa: BLE001
            raise ApplicationError(
                f"social_metrics_failed at step={step}: {exc!r}",
                non_retryable=True,
            ) from exc

        return {
            "refreshed": result.get("refreshed", 0),
            "failed": result.get("failed", 0),
        }
