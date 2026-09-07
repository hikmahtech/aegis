"""DeliveryWatchdogFlow — catch silently-undelivered interaction cards
and detect comms inbound-channel outages.

Interaction rows are created BEFORE the card is dispatched, so a row with
neither `telegram_message_id` (legacy column) nor `delivery_ref` set past a
grace window was never delivered. Previously the only way to notice was a
manual SQL query; this flow surfaces it automatically.

Each run also checks the comms service's /api/health endpoint for the
`inbound.healthy` flag. Both findings go to the problem hub
(`reconcile_findings`): an `undelivered_cards` problem on subject
`interactions` and a `comms_inbound_down` problem on subject `polling`, kind
`comms`. The hub dedupes and resolves — so a sustained outage is one problem
(and, projected, one Todoist task: Todoist rather than the chat channel,
because the chat channel is the thing that is down), and the undelivered
summary card is sent once per incident instead of once per hour.
"""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.homelab import HomelabActivities
    from aegis_worker.activities.hub import HubActivities
    from aegis_worker.shared.retry import FAST, NO_RETRY, TIMEOUT_FAST, TIMEOUT_STANDARD


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

        # Best-effort comms inbound health check — never fails the watchdog run.
        # Recorded into result_summary (issue #120): this half of the watchdog
        # has caught real Slack outages and must stay visible to audits.
        result: dict = {"undelivered": len(rows), "comms_inbound_status": "unknown"}
        health: dict = {"status": "unknown"}
        try:
            health = await workflow.execute_activity_method(
                HomelabActivities.check_comms_inbound_health,
                args=[config.comms_url],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
            result["comms_inbound_status"] = health.get("status", "unknown")
        except Exception:
            result["comms_inbound_status"] = "check_failed"

        findings: list[dict] = []
        if rows:
            by_origin: dict[str, int] = {}
            for r in rows:
                key = r.get("origin") or "?"
                by_origin[key] = by_origin.get(key, 0) + 1
            findings.append(
                {
                    "klass": "undelivered_cards",
                    "subject": "interactions",
                    "title": f"{len(rows)} undelivered interaction card(s)",
                    "severity": "warning",
                    "payload": {"count": len(rows), "by_origin": by_origin},
                }
            )
        # `unknown` / `check_failed` says nothing about inbound, so the comms
        # class is neither reported nor resolved on such a tick.
        classes = ["undelivered_cards"]
        if health.get("status") in ("ok", "down"):
            classes.append("comms_inbound_down")
        if health.get("status") == "down":
            ago = health.get("last_ok_seconds_ago")
            okstr = "never" if ago is None else f"{ago}s ago"
            findings.append(
                {
                    "klass": "comms_inbound_down",
                    "subject": "polling",
                    "title": (
                        f"\U0001f6a8 AEGIS inbound comms is DOWN (last ok {okstr})"
                        " — button taps and messages are not being received"
                    ),
                    "severity": "critical",
                    "payload": {
                        "last_ok_seconds_ago": ago,
                        "last_error": health.get("last_error"),
                    },
                }
            )

        try:
            outcome = await workflow.execute_activity_method(
                HubActivities.reconcile_findings,
                args=[
                    {
                        "source": "delivery",
                        "subject_kind": "comms",
                        "classes": classes,
                        "findings": findings,
                    }
                ],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=NO_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 — a hub outage must not hide the findings
            workflow.logger.warning("delivery_watchdog_hub_failed err=%s", str(exc)[:200])
            outcome = {"fresh": findings, "resolved": []}

        fresh = {f.get("klass") for f in outcome.get("fresh") or []}
        resolved = {r.get("klass") for r in outcome.get("resolved") or []}
        if "undelivered_cards" in fresh and not config.silent:
            await workflow.execute_activity_method(
                HomelabActivities.notify_undelivered_interactions,
                args=[rows],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
        if health.get("status") == "down":
            result["comms_inbound_alerted"] = "comms_inbound_down" in fresh
        if "comms_inbound_down" in resolved:
            result["comms_inbound_resolved"] = True
        return result
