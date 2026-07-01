"""DeliveryWatchdogFlow — catch silently-undelivered Telegram interaction cards
and detect Telegram-API polling outages.

Interaction rows are created BEFORE the Telegram card is dispatched, so a row
whose `telegram_message_id` stays NULL past a grace window was never delivered
(e.g. BUTTON_DATA_INVALID when callback_data exceeds Telegram's 64-byte cap).
Previously the only way to notice was a manual SQL query; this flow surfaces it
automatically.

Additionally, each 15-min run checks the Telegram service's /api/health endpoint
for the `telegram_api.reachable` flag.  When it is False the flow creates a
Todoist Inbox task (via Todoist, not Telegram — which is the thing that's down)
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
    telegram_url: str = ""  # passed to check_telegram_polling_health


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

        # Best-effort Telegram polling health check — never fails the watchdog run.
        try:
            health = await workflow.execute_activity_method(
                HomelabActivities.check_telegram_polling_health,
                args=[config.telegram_url],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
            if health.get("status") == "down":
                await workflow.execute_activity_method(
                    HomelabActivities.alert_telegram_polling_down,
                    args=[health.get("last_ok_seconds_ago"), health.get("last_error")],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
        except Exception:
            pass  # polling check is best-effort; never fails the watchdog

        return {"undelivered": len(rows)}
