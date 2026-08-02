"""SocialMetricsFlow — pull Postiz analytics into social_outbox.metrics, then
check that nothing Postiz accepted is silently sitting unpublished.

Daily. Two halves:

1. `refresh_post_metrics` walks Postiz-routed `social_outbox` rows and caches
   each post's latest analytics series (likes/comments/…) plus Postiz's own
   state/release_url/publish_date onto the row. No gate on
   `social_publishing_enabled` — metrics on already-posted rows are harmless to
   refresh even while publishing itself is switched off.
2. the **stuck-post watchdog** (#225): any Postiz-routed row whose `schedule_at`
   is hours past while Postiz has not confirmed it PUBLISHED gets one deduped
   card. Between 2026-07-29 07:30 and 2026-08-02 13:01 UTC not a single social
   post published — Postiz's Temporal worker came back from a restart with zero
   pollers — and nothing told anyone for four days. `social_outbox` said
   `posted` (the handoff really did succeed) while Postiz's state stayed QUEUE.

The watchdog lives **inside this flow rather than in a flow of its own** on
purpose. Its whole input is `metrics.state`, which only step 1 produces, so
running it anywhere else means either checking a stale snapshot or duplicating
the Postiz calls. Sharing the flow also means no new schedule, no new seed row,
and detection that is by construction reading a state fetched seconds earlier.
Its detection/alert split, `audit_log` dedup, `alert_mutes` support and
recovery-notice re-arm all follow `FlowHealthWatchdogFlow` (#226).
"""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.social import SocialActivities
    from aegis_worker.shared.retry import FAST, NO_RETRY, TIMEOUT_FAST, TIMEOUT_STANDARD


@dataclass
class SocialMetricsConfig:
    agent_id: str
    #: Backward half of the Postiz `GET /posts` window, and the recency bound on
    #: which outbox rows are refreshed.
    window_days: int = 14
    #: Forward half of that window. Was hardcoded at 1 day, which made every
    #: post scheduled further out than tomorrow read as `state: "unknown"`.
    #: 45 covers the longest real campaign (30 days) plus authoring lead time.
    lookahead_days: int = 45
    #: Per-pass cap on Postiz analytics calls (one per row). Hitting it is
    #: logged as a warning, never swallowed.
    max_rows: int = 200
    #: A post is stuck once it is this many hours past `schedule_at` without a
    #: PUBLISHED confirmation. A false-positive margin, not a detection budget —
    #: the daily cadence sets latency.
    stuck_after_hours: int = 6
    max_stuck: int = 50
    #: Above the 24h cadence on purpose: a shorter window would never dedup.
    dedup_hours: int = 168
    recovery_hours: int = 720
    check_stuck: bool = True


@workflow.defn(name="SocialMetricsFlow")
class SocialMetricsFlow:
    @workflow.run
    async def run(self, config: SocialMetricsConfig) -> dict:
        step = "refresh_post_metrics"
        try:
            result = await workflow.execute_activity_method(
                SocialActivities.refresh_post_metrics,
                args=[config.window_days, config.lookahead_days, config.max_rows],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=NO_RETRY,
            )
        except Exception as exc:  # noqa: BLE001
            raise ApplicationError(
                f"social_metrics_failed at step={step}: {exc!r}",
                non_retryable=True,
            ) from exc

        out = {
            "refreshed": result.get("refreshed", 0),
            "failed": result.get("failed", 0),
        }
        if not config.check_stuck:
            return {**out, "stuck_status": "disabled"}

        # Best-effort: a broken watchdog must not fail the metrics refresh that
        # already succeeded, but it is recorded so it cannot rot unnoticed.
        try:
            stuck = await workflow.execute_activity_method(
                SocialActivities.find_stuck_posts,
                args=[config.stuck_after_hours, config.max_stuck],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=FAST,
            )
        except Exception:  # noqa: BLE001 — degrade, never take the run down
            return {**out, "stuck_status": "check_failed"}

        # Reporting is SKIPPED (not called with []) when detection failed —
        # an empty findings list is indistinguishable from "everything
        # recovered" and would fire bogus recovery notices that also re-arm
        # dedup, so the next real alert would be delayed by a whole cycle.
        report = await workflow.execute_activity_method(
            SocialActivities.report_stuck_posts,
            args=[stuck, config.agent_id, config.dedup_hours, config.recovery_hours],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=NO_RETRY,
        )
        return {**out, "stuck": len(stuck), "stuck_status": "ok", **report}
