"""MoneyHygieneDailyFlow — daily recurring-charge sweep.

Merges the former CancellationScanFlow + RenewalRadarFlow. Both ran daily off
the same `recurring_charge` table for near-zero daily yield, so they are one
flow now with two independent sweeps — a failure in one does not block the
other. `silent` suppresses the user-facing capture+notify for both (the DB
state changes still happen).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.capture import CaptureActivities
    from aegis_worker.activities.money import MoneyActivities
    from aegis_worker.shared.retry import FAST, NO_RETRY, TIMEOUT_FAST

_ACT_TIMEOUT = timedelta(seconds=60)


@dataclass
class MoneyHygieneConfig:
    agent_id: str = "maou"
    silent: bool = False
    threshold_multiplier: float = 2.0
    thresholds_days: list[int] = field(default_factory=lambda: [30, 14, 7, 0])


@workflow.defn(name="MoneyHygieneDailyFlow")
class MoneyHygieneDailyFlow:
    @workflow.run
    async def run(self, config: MoneyHygieneConfig) -> dict:
        return {
            "cancelled": await self._scan_cancellations(config),
            "renewals": await self._scan_renewals(config),
        }

    async def _scan_cancellations(self, config: MoneyHygieneConfig) -> int:
        """Flag active charges with no recent receipt as cancelled."""
        try:
            cancellations = (
                await workflow.execute_activity(
                    "detect_cancellations",
                    config.threshold_multiplier,
                    start_to_close_timeout=_ACT_TIMEOUT,
                    retry_policy=NO_RETRY,
                )
                or []
            )
        except Exception as exc:
            workflow.logger.error("money_hygiene_cancellations_failed err=%s", str(exc)[:200])
            return 0

        if config.silent:
            return len(cancellations)

        for cancel in cancellations:
            sub_id = str(cancel.get("id", ""))
            if not sub_id:
                continue
            vendor = cancel.get("vendor_name") or "subscription"
            amount_fmt = f"{(cancel.get('amount_cents') or 0) / 100:.2f}"
            currency = cancel.get("currency") or "?"
            cadence = cancel.get("cadence") or "?"
            last_seen = cancel.get("last_seen_at")
            last_date = str(last_seen)[:10] if last_seen else "unknown"
            try:
                await workflow.execute_activity_method(
                    CaptureActivities.capture_to_inbox,
                    args=[
                        "#receipt",
                        f"cancel-{sub_id}",
                        f"Possible cancellation: {vendor}"[:120],
                        f"Last seen: {last_date} ({currency} {amount_fmt}, {cadence})",
                    ],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:
                workflow.logger.warning(
                    "cancel_capture_failed sub_id=%s err=%s", sub_id, str(exc)[:200]
                )
            try:
                await workflow.execute_activity_method(
                    MoneyActivities.notify_cancellation,
                    args=[cancel],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            except Exception:
                continue
        return len(cancellations)

    async def _scan_renewals(self, config: MoneyHygieneConfig) -> int:
        """Alert on charges crossing a renewal threshold."""
        try:
            alerts = await workflow.execute_activity_method(
                MoneyActivities.evaluate_renewal_alerts,
                args=[config.thresholds_days],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=FAST,
            )
        except Exception as exc:
            workflow.logger.error("money_hygiene_renewals_failed err=%s", str(exc)[:200])
            return 0

        if config.silent:
            return len(alerts)

        for a in alerts:
            charge_id = a.get("charge_id")
            if charge_id:
                next_due_at = a.get("next_due_at", "?")
                try:
                    await workflow.execute_activity_method(
                        CaptureActivities.capture_to_inbox,
                        args=[
                            "#receipt",
                            f"renewal-{charge_id}-{next_due_at}",
                            f"Renewal in {a.get('days_left', '?')} days: "
                            f"{a.get('vendor_name', 'subscription')} "
                            f"({a.get('amount_cents', '?')} {a.get('currency', '')})"[:120],
                            f"{a.get('account', '')}\nNext charge: {next_due_at}",
                        ],
                        start_to_close_timeout=TIMEOUT_FAST,
                        retry_policy=NO_RETRY,
                    )
                except Exception as exc:
                    workflow.logger.warning(
                        "renewal_capture_failed charge_id=%s err=%s", charge_id, str(exc)[:200]
                    )
            try:
                await workflow.execute_activity_method(
                    MoneyActivities.notify_renewal_alert,
                    args=[a],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            except Exception:
                continue
        return len(alerts)
