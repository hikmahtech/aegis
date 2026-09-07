"""CertRadarFlow - daily TLS expiry probe for public domains."""

from __future__ import annotations

from dataclasses import dataclass, field

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.homelab import HomelabActivities
    from aegis_worker.activities.hub import HubActivities
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
        findings: list[dict] = []
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
                if alert is None:
                    continue
                # One `cert_expiring` / `cert_unreachable` problem per domain
                # on the hub; the 14→7→0 day crossings are occurrences on it,
                # and a renewed cert (no finding) resolves it.
                if alert.get("unreachable"):
                    findings.append(
                        {
                            "klass": "cert_unreachable",
                            "subject": domain,
                            "title": f"TLS probe of {domain} failed",
                            "severity": "warning",
                            "payload": {"error": alert.get("error", "")},
                        }
                    )
                else:
                    findings.append(
                        {
                            "klass": "cert_expiring",
                            "subject": domain,
                            "title": f"Certificate for {domain} expires in {alert['days']} day(s)",
                            "severity": "critical" if alert["days"] <= 7 else "warning",
                            "payload": {k: v for k, v in alert.items() if k != "domain"},
                        }
                    )
                if config.silent:
                    continue
                alerts += 1
                # notify_cert_alert intentionally does NOT use
                # safe_send_message (see homelab.py:notify_cert_alert);
                # it manages a sticky `last_alert_threshold` and emits its
                # own ERROR log. It CAN still raise on activity-runtime
                # issues, so keep this try/except as a backstop. Every
                # threshold crossing is carded (they escalate), whether or
                # not the hub already holds the problem.
                try:
                    await workflow.execute_activity_method(
                        HomelabActivities.notify_cert_alert,
                        args=[alert],
                        start_to_close_timeout=TIMEOUT_FAST,
                        retry_policy=NO_RETRY,
                    )
                except Exception:
                    pass
            if config.domains:
                try:
                    await workflow.execute_activity_method(
                        HubActivities.reconcile_findings,
                        args=[
                            {
                                "source": "expiry",
                                "subject_kind": "domain",
                                "classes": ["cert_expiring", "cert_unreachable"],
                                "findings": findings,
                            }
                        ],
                        start_to_close_timeout=TIMEOUT_STANDARD,
                        retry_policy=NO_RETRY,
                    )
                except Exception as exc:  # noqa: BLE001 — the cards already went out
                    workflow.logger.warning("cert_radar_hub_failed err=%s", str(exc)[:200])
        except Exception as exc:
            workflow.logger.error("cert_radar_failed error=%s", str(exc)[:200])
            raise
        return {"alerts": alerts, "problems": len(findings)}
