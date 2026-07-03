"""DeliveryWatchdogFlow — catch silently-undelivered interaction cards
and detect comms inbound-channel outages.

Interaction rows are created BEFORE the card is dispatched, so a row with
neither `telegram_message_id` (legacy column) nor `delivery_ref` set past a
grace window was never delivered.
Previously the only way to notice was a manual SQL query; this flow surfaces it
automatically.

Additionally, each 15-min run checks the comms service's /api/health endpoint
for the `inbound.healthy` flag.  When it is False the flow creates a Todoist
Inbox task (via Todoist, not the chat channel — which is the thing that's down)
so the outage is visible without relying on the broken channel.  A 12-hour dedup
window prevents task spam during a sustained outage.
"""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.homelab import HomelabActivities
    from aegis_worker.shared.retry import FAST, NO_RETRY, TIMEOUT_FAST


@dataclass
class DeliveryWatchdogConfig:
    silent: bool = False  # if True, detect but don't notify
    threshold_seconds: int = 120  # grace period before a NULL id counts as undelivered
    window_hours: int = 24  # ignore older rows (retired origins)
    comms_url: str = ""  # passed to check_comms_inbound_health


@workflow.defn
class DeliveryWatchdogFlow:
    @workflow.run
    async def run(self, config: DeliveryWatchdogConfig) -> dict:
        rows = await workflow.execute_activity_method(
            HomelabActivities.find_undelivered_interactions,
            args=[config.threshold_seconds, config.window_hours],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=FAST,
        )
        if rows and not config.silent:
            await workflow.execute_activity_method(
                HomelabActivities.notify_undelivered_interactions,
                args=[rows],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )

        # Best-effort comms inbound health check — never fails the watchdog run.
        try:
            health = await workflow.execute_activity_method(
                HomelabActivities.check_comms_inbound_health,
                args=[config.comms_url],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
            if health.get("status") == "down":
                await workflow.execute_activity_method(
                    HomelabActivities.alert_comms_inbound_down,
                    args=[health.get("last_ok_seconds_ago"), health.get("last_error")],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
        except Exception:
            pass  # polling check is best-effort; never fails the watchdog

        return {"undelivered": len(rows)}
