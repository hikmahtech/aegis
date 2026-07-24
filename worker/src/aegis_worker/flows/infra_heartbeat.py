"""InfraHeartbeatFlow — 2-min swarm liveness/convergence poll (spec 2026-07-24).

Emits ONLY on state transitions, never steady state:
- node → Down (first sight counts)          → NodeDown alert (escalate)
- node Down → Ready                          → resolved audit row
- service stuck 2 consecutive ticks          → DockerServiceDown alert
  (routes into the existing auto-remediation in AlertInvestigationFlow)
- confirmed-stuck service converged          → resolved audit row
- collect failed `fail_threshold` in a row   → HeartbeatCollectFailed alert
- collect recovered                          → its resolved audit row

Recovery writes the same audit_log row shape the Alertmanager webhook writes
(action=alert_received, details.resolved=true), so check_alert_resolved and
the whole self-resolve machinery work unchanged for heartbeat alerts.

Children are spawned ABANDONED (sentry_poll pattern) — investigations carry
human gates and must outlive this 2-min tick. Dead-man ping fires only on a
successful collect so a dead AEGIS/node silences healthchecks.io.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.homelab import HomelabActivities
    from aegis_worker.flows.alert_investigation import AlertInvestigationFlow
    from aegis_worker.shared.retry import FAST, NO_RETRY, TIMEOUT_FAST, TIMEOUT_STANDARD


def _safe_id_segment(text: str, max_len: int = 60) -> str:
    return re.sub(r"[^a-zA-Z0-9._\-]", "-", text)[:max_len]


def _hb_fingerprint(alertname: str, subject: str) -> str:
    return f"aegis-heartbeat:{alertname}:{subject}"


def build_heartbeat_alert(
    alertname: str,
    subject: str,
    infra_cluster: str,
    title: str,
    description: str,
    *,
    escalate: bool,
    service_name: str = "",
) -> dict:
    labels: dict = {"alertname": alertname}
    if infra_cluster:
        labels["cluster"] = infra_cluster
    if service_name:
        labels["service_name"] = service_name
    alert: dict = {
        "title": title,
        "description": description,
        "source": "aegis-heartbeat",
        "severity": "critical",
        "fingerprint": _hb_fingerprint(alertname, subject),
        "labels": labels,
        "escalate": escalate,
    }
    if service_name:
        alert["service"] = service_name
    return alert


@dataclass
class InfraHeartbeatConfig:
    agent_id: str = "pandoras-actor"
    fail_threshold: int = 3


@workflow.defn
class InfraHeartbeatFlow:
    async def _spawn(self, alert: dict) -> bool:
        """ABANDONED child; an already-running same-id child is benign."""
        child_id = (
            f"aegis-heartbeat-{_safe_id_segment(alert['labels']['alertname'].lower())}-"
            f"{_safe_id_segment(alert['fingerprint'].rsplit(':', 1)[-1])}"
        )
        try:
            await workflow.start_child_workflow(
                AlertInvestigationFlow.run,
                alert,
                id=child_id,
                parent_close_policy=workflow.ParentClosePolicy.ABANDON,
            )
            return True
        except Exception as exc:  # noqa: BLE001 — already-started dedup is benign
            workflow.logger.warning(
                "heartbeat_spawn_skipped id=%s err=%s", child_id, str(exc)[:200]
            )
            return False

    @workflow.run
    async def run(self, config: InfraHeartbeatConfig) -> dict:
        prior = await workflow.execute_activity_method(
            HomelabActivities.read_heartbeat_state,
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=FAST,
        )
        current = await workflow.execute_activity_method(
            HomelabActivities.collect_infra_state,
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=FAST,
        )
        routing = await workflow.execute_activity_method(
            HomelabActivities.get_heartbeat_routing,
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=NO_RETRY,
        )
        cluster = routing.get("infra_cluster") or ""
        spawned = 0

        # ── Collect failure path ──
        if not current.get("ok"):
            fail_count = int(prior.get("fail_count") or 0) + 1
            if fail_count == config.fail_threshold:
                alert = build_heartbeat_alert(
                    "HeartbeatCollectFailed",
                    "collect",
                    cluster,
                    "Infra heartbeat cannot reach the swarm",
                    f"{fail_count} consecutive collect failures. "
                    f"Last error: {current.get('error', '')}",
                    escalate=True,
                )
                if await self._spawn(alert):
                    spawned += 1
            await workflow.execute_activity_method(
                HomelabActivities.write_heartbeat_state,
                args=[{**prior, "fail_count": fail_count}],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=FAST,
            )
            return {"collect_ok": False, "alerts_spawned": spawned, "fail_count": fail_count}

        # ── Success path: diff transitions ──
        prev_nodes: dict = prior.get("nodes") or {}
        cur_nodes: dict = current.get("nodes") or {}
        prev_stuck = set(prior.get("stuck") or [])
        prev_confirmed = set(prior.get("confirmed") or [])
        cur_stuck = set(current.get("stuck") or [])

        nodes_down, nodes_recovered = [], []
        for name, status in cur_nodes.items():
            if status == "Down" and prev_nodes.get(name) != "Down":
                nodes_down.append(name)
            elif status == "Ready" and prev_nodes.get(name) == "Down":
                nodes_recovered.append(name)

        new_confirmed = (cur_stuck & prev_stuck) - prev_confirmed
        confirmed_now = (prev_confirmed | new_confirmed) & cur_stuck
        recovered_services = prev_confirmed - cur_stuck

        for node in nodes_down:
            alert = build_heartbeat_alert(
                "NodeDown",
                node,
                cluster,
                f"Swarm node {node} down",
                f"Heartbeat poll saw node {node} transition to Down.",
                escalate=True,
            )
            if await self._spawn(alert):
                spawned += 1
        for svc in sorted(new_confirmed):
            alert = build_heartbeat_alert(
                "DockerServiceDown",
                svc,
                cluster,
                f"Service {svc} down",
                f"Heartbeat poll saw {svc} below desired replicas for 2 consecutive ticks.",
                escalate=False,
                service_name=svc,
            )
            if await self._spawn(alert):
                spawned += 1

        for node in nodes_recovered:
            await workflow.execute_activity_method(
                HomelabActivities.record_heartbeat_resolved,
                args=[_hb_fingerprint("NodeDown", node)],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
        for svc in sorted(recovered_services):
            await workflow.execute_activity_method(
                HomelabActivities.record_heartbeat_resolved,
                args=[_hb_fingerprint("DockerServiceDown", svc)],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
        if int(prior.get("fail_count") or 0) >= config.fail_threshold:
            await workflow.execute_activity_method(
                HomelabActivities.record_heartbeat_resolved,
                args=[_hb_fingerprint("HeartbeatCollectFailed", "collect")],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )

        await workflow.execute_activity_method(
            HomelabActivities.write_heartbeat_state,
            args=[
                {
                    "nodes": cur_nodes,
                    "stuck": sorted(cur_stuck),
                    "confirmed": sorted(confirmed_now),
                    "fail_count": 0,
                }
            ],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=FAST,
        )
        await workflow.execute_activity_method(
            HomelabActivities.ping_deadman,
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=NO_RETRY,
        )
        return {
            "collect_ok": True,
            "alerts_spawned": spawned,
            "nodes_down": len(nodes_down),
            "nodes_recovered": len(nodes_recovered),
            "services_confirmed_stuck": len(new_confirmed),
            "services_recovered": len(recovered_services),
        }
