"""CertRadarFlow - daily TLS expiry probe for public domains.

Two outputs with two different triggers:

* the hub finding is level-triggered. A cert inside 14 days, or a domain the
  probe cannot reach, is a finding on every run, because
  `reconcile_findings` reads a finding that goes missing as recovery. A
  renewed cert produces none, and that is what resolves its problem (#475).
* the Slack card is edge-triggered. It goes out when the probe crosses a
  14/7/0-day mark for the first time on a cert (the sticky
  `last_alert_threshold` in `pandoras_actor.cert_expiry`), and on every
  unreachable probe, as it always has.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.homelab import HomelabActivities
    from aegis_worker.activities.hub import HubActivities
    from aegis_worker.shared.retry import FAST, NO_RETRY, TIMEOUT_FAST, TIMEOUT_STANDARD

# Inside the widest card threshold a cert is a finding every day.
_EXPIRING_WITHIN_DAYS = max(HomelabActivities._CERT_THRESHOLDS)


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
                    report = await workflow.execute_activity_method(
                        HomelabActivities.probe_and_upsert_cert,
                        args=[domain],
                        start_to_close_timeout=TIMEOUT_STANDARD,
                        retry_policy=FAST,
                    )
                except Exception:
                    continue
                # Replay: before #475 the probe returned None on a day nothing
                # was crossed, and a dict only on a crossing or an unreachable
                # probe. The code below takes both old shapes down the path
                # the old code did, so a replay issues the same commands.
                if report is None:
                    continue
                # One `cert_expiring` / `cert_unreachable` problem per domain
                # on the hub: every run inside the window is an occurrence on
                # it, and a renewed cert (no finding) resolves it.
                unreachable = bool(report.get("unreachable"))
                if unreachable:
                    findings.append(
                        {
                            "klass": "cert_unreachable",
                            "subject": domain,
                            "title": f"TLS probe of {domain} failed",
                            "severity": "warning",
                            "payload": {"error": report.get("error", "")},
                        }
                    )
                # An unreachable report carries the last cert seen, when there
                # was one: an outage is no evidence of a renewal.
                days = report.get("days")
                if days is not None and days <= _EXPIRING_WITHIN_DAYS:
                    # The date, not a day count: the problem keeps the title
                    # it opened with, and it now stays open for days.
                    expires_on = str(report["not_after"])[:10]
                    findings.append(
                        {
                            "klass": "cert_expiring",
                            "subject": domain,
                            "title": f"Certificate for {domain} expires on {expires_on}",
                            "severity": "critical" if days <= 7 else "warning",
                            "payload": {k: v for k, v in report.items() if k != "domain"},
                        }
                    )
                # The card goes out on a first crossing and on every
                # unreachable probe. A quiet day inside the window is a
                # finding and nothing more.
                if not unreachable and report.get("threshold") is None:
                    continue
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
                        args=[report],
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
