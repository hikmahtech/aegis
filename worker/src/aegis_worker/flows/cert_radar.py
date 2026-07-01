"""CertRadarFlow - daily TLS expiry probe for public domains."""

from __future__ import annotations

from dataclasses import dataclass, field

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.homelab import HomelabActivities
    from aegis_worker.shared.retry import FAST, NO_RETRY, TIMEOUT_FAST, TIMEOUT_STANDARD


@dataclass
class CertRadarConfig:
    silent: bool = False
    domains: list[str] = field(default_factory=list)


@workflow.defn
class CertRadarFlow:
    @workflow.run
    async def run(self, config: CertRadarConfig) -> dict:
        alerts = 0
        try:
            for domain in config.domains:
                try:
                    alert = await workflow.execute_activity_method(
                        HomelabActivities.probe_and_upsert_cert,
                        args=[domain],
                        start_to_close_timeout=TIMEOUT_STANDARD,
                        retry_policy=FAST,
                    )
                except Exception:
                    continue
                if alert is None or config.silent:
                    continue
                alerts += 1
                # notify_cert_alert intentionally does NOT use
                # safe_send_telegram (see homelab.py:notify_cert_alert);
                # it manages a sticky `last_alert_threshold` and emits its
                # own ERROR log. It CAN still raise on activity-runtime
                # issues, so keep this try/except as a backstop.
                try:
                    await workflow.execute_activity_method(
                        HomelabActivities.notify_cert_alert,
                        args=[alert],
                        start_to_close_timeout=TIMEOUT_FAST,
                        retry_policy=NO_RETRY,
                    )
                except Exception:
                    pass
        except Exception as exc:
            workflow.logger.error("cert_radar_failed error=%s", str(exc)[:200])
            raise
        return {"alerts": alerts}
