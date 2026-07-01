"""ServiceDriftFlow - hourly Docker Swarm state drift check."""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.homelab import HomelabActivities
    from aegis_worker.shared.retry import FAST, NO_RETRY, TIMEOUT_FAST, TIMEOUT_STANDARD


@dataclass
class ServiceDriftConfig:
    silent: bool = False  # if True, persist but don't notify (rollout phase 1)
    # Wait-and-watch: when drift is seen, sleep this long and re-check before
    # alerting. Suppresses services caught mid-rollout / mid-restart and batch
    # jobs that just finished. 0 disables the re-check (immediate notify).
    recheck_delay_seconds: int = 120


@workflow.defn
class ServiceDriftFlow:
    @workflow.run
    async def run(self, config: ServiceDriftConfig) -> dict:
        drifts_new = 0
        resolved = 0
        suppressed = 0
        today = workflow.now().strftime("%Y-%m-%d")
        try:
            collected = await workflow.execute_activity_method(
                HomelabActivities.collect_services,
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=FAST,
            )
            drifts = _compute_drift_inline(collected, today)

            # Wait-and-watch: a single hourly snapshot catches services that are
            # momentarily at 0 replicas mid-rollout/restart, or batch jobs that
            # just finished — both clear on their own. Re-check after a delay and
            # keep only drifts STILL present, so transient blips never alert.
            if drifts and config.recheck_delay_seconds > 0:
                await workflow.sleep(config.recheck_delay_seconds)
                recheck = await workflow.execute_activity_method(
                    HomelabActivities.collect_services,
                    start_to_close_timeout=TIMEOUT_STANDARD,
                    retry_policy=FAST,
                )
                still = {
                    (d["service_name"], d["drift_type"])
                    for d in _compute_drift_inline(recheck, today)
                }
                confirmed = [d for d in drifts if (d["service_name"], d["drift_type"]) in still]
                suppressed = len(drifts) - len(confirmed)
                drifts = confirmed

            drifts_new = await workflow.execute_activity_method(
                HomelabActivities.persist_drifts,
                args=[drifts],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=FAST,
            )
            still_open = [d["alert_key"] for d in drifts]
            resolved = await workflow.execute_activity_method(
                HomelabActivities.resolve_stale_drifts,
                args=[still_open],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=FAST,
            )
            if not config.silent:
                for d in drifts:
                    # notify_drift uses safe_send_telegram internally and
                    # never raises — no wrapping try/except needed.
                    await workflow.execute_activity_method(
                        HomelabActivities.notify_drift,
                        args=[d],
                        start_to_close_timeout=TIMEOUT_FAST,
                        retry_policy=NO_RETRY,
                    )
        except Exception as exc:
            workflow.logger.error("service_drift_failed error=%s", str(exc)[:200])
            raise
        return {"drifts_new": drifts_new, "resolved": resolved, "suppressed": suppressed}


def _compute_drift_inline(collected: dict, today: str = "") -> list[dict]:
    """Pure function - same shape as HomelabActivities.compute_drift so it
    can run inside the workflow context without a side-effecting activity.
    Returns list[dict] (DriftRecord serialized).
    today: ISO date string (YYYY-MM-DD) passed from workflow.now() to avoid
    calling date.today() inside the workflow sandbox."""
    if not today:
        from datetime import date

        today = date.today().isoformat()
    oom_patterns = ("non-zero exit (137)", "killed", "OOMKilled")
    services = collected["services"]
    ps_map = collected["ps_map"]
    out = []
    for s in services:
        name = s["name"]
        if s["replicas_actual"] < s["replicas_desired"]:
            out.append(
                {
                    "service_name": name,
                    "stack_name": s["stack"],
                    "drift_type": "replicas",
                    "expected": {"desired": s["replicas_desired"]},
                    "actual": {"actual": s["replicas_actual"]},
                    "severity": "critical" if s["replicas_actual"] == 0 else "warn",
                    "alert_key": f"{name}:replicas:{today}",
                }
            )
        # Only inspect the most recent task — `docker service ps` orders
        # newest-first, so tasks[1:] is stale history. Flagging any OOM in
        # history produces false positives for services that crashed weeks
        # ago and have been healthy since.
        tasks = ps_map.get(name, [])
        if tasks:
            t = tasks[0]
            err = t.get("error", "") or ""
            current = t.get("current_state", "") or ""
            # Skip when the current task is running cleanly — old OOMs in
            # tasks[1:] are the normal swarm history of a healthy service.
            is_healthy = (
                t.get("desired_state") == "Running" and current.startswith("Running") and not err
            )
            if not is_healthy and any(p in err for p in oom_patterns):
                out.append(
                    {
                        "service_name": name,
                        "stack_name": s["stack"],
                        "drift_type": "oom_exit",
                        "expected": {"exit": "clean"},
                        "actual": {"task_id": t["task_id"], "error": err[:200]},
                        "severity": "critical",
                        "alert_key": f"{name}:oom_exit:{today}",
                    }
                )
    return out
