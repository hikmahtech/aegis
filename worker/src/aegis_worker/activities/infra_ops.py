"""InfraOpsActivities — swarm service ops as Temporal activities.

`HomelabConnector` already implements these (core/src/aegis/connectors/homelab.py),
but only chat tools and the alert-remediation path call it directly. Workflows
cannot touch connectors from within workflow code, so they need activity
wrappers. Every `HomelabConnector` method returns the connector's standard
envelope — `{"ok": bool, "data": Any, "error": str|None, "retryable": bool,
"external_ref": str|None}` — which these activities unwrap into the plain
result shapes this flow needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from temporalio import activity


@dataclass
class InfraOpsActivities:
    homelab_connector: Any = None

    @activity.defn
    async def service_health(self, service_name: str) -> dict:
        """Is `service_name` running its desired replica count right now?

        A missing service is `found: False` — never silently "healthy", or a
        renamed service would auto-close its own alert task.
        """
        if self.homelab_connector is None or not service_name:
            return {"found": False, "healthy": False, "detail": "no connector or service name"}
        try:
            env = await self.homelab_connector.list_services()
        except Exception as exc:  # noqa: BLE001
            activity.logger.warning("service_health_failed err=%s", str(exc)[:200])
            return {"found": False, "healthy": False, "detail": str(exc)[:200]}
        if not env.get("ok"):
            return {"found": False, "healthy": False, "detail": str(env.get("error"))[:200]}

        for svc in env.get("data") or []:
            if str(svc.get("name", "")).lower() != service_name.lower():
                continue
            desired = svc.get("replicas_desired") or 0
            actual = svc.get("replicas_actual") or 0
            healthy = desired > 0 and actual >= desired
            return {"found": True, "healthy": healthy, "detail": f"{actual}/{desired}"}
        return {"found": False, "healthy": False, "detail": "service not in swarm"}

    @activity.defn
    async def service_logs(self, service_name: str, lines: int = 50) -> dict:
        """Recent task state for `service_name` — the closest thing to "logs"
        `HomelabConnector` exposes (there is no `docker service logs` wrapper;
        `service_ps`'s per-task current/desired state + error is the diagnostic
        signal used elsewhere, e.g. `alerts.py::remediate_infra_service`)."""
        if self.homelab_connector is None or not service_name:
            return {"logs": ""}
        try:
            env = await self.homelab_connector.service_ps(service_name)
        except Exception as exc:  # noqa: BLE001
            activity.logger.warning("service_logs_failed err=%s", str(exc)[:200])
            return {"logs": ""}
        if not env.get("ok"):
            return {"logs": str(env.get("error") or "")[:2000]}

        rows = []
        for t in (env.get("data") or [])[:lines]:
            rows.append(
                f"{t.get('current_state', '')} (desired={t.get('desired_state', '')}) "
                f"node={t.get('node', '')} err={t.get('error', '')}"
            )
        return {"logs": "\n".join(rows)[:4000]}

    @activity.defn
    async def restart_service(self, service_name: str) -> dict:
        if self.homelab_connector is None or not service_name:
            return {"ok": False, "detail": "no connector or service name"}
        try:
            env = await self.homelab_connector.restart_service(service_name)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": str(exc)[:200]}
        if not env.get("ok"):
            return {"ok": False, "detail": str(env.get("error"))[:500]}
        return {"ok": True, "detail": str(env.get("data") or "")[:500]}
