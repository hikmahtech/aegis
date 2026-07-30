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


def resolve_swarm_service(query: str, names: list[str]) -> tuple[str, str]:
    """Map an alert's service token onto a real swarm service name.

    Two alert-title shapes reach the infra verb and only one carries a real
    identifier. `PROLONGED: clickhouse_clickhouse degraded` names the compose
    service; `PostgreSQL is down` names the PRODUCT (Prometheus rules use a
    human-readable summary), and `docker service update --force postgresql`
    fails with "no such service" (issue #166, hit in prod 2026-07-30).

    Ordered, and deliberately refuses to guess:
      1. exact match (case-insensitive)
      2. the compose convention `{name}_{name}`  — loki -> loki_loki
      3. a UNIQUE `{name}_` prefix match         — postgresql -> postgresql_postgres
      4. otherwise ""

    Step 4 is load-bearing: `clickhouse` prefix-matches clickhouse_clickhouse,
    -backup and -exporter. Restarting the wrong one is worse than doing
    nothing — the same rule repo resolution follows (#158).

    Returns (resolved_name, reason). `reason` is "" on an exact hit and
    otherwise explains the mapping, so callers can say which service they
    actually acted on rather than which one the alert named.
    """
    q = (query or "").strip().lower()
    if not q:
        return "", "no service name"
    by_lower = {n.lower(): n for n in names}

    if q in by_lower:
        return by_lower[q], ""

    convention = f"{q}_{q}"
    if convention in by_lower:
        return by_lower[convention], f"{query} -> {by_lower[convention]} (compose convention)"

    prefixed = [orig for low, orig in by_lower.items() if low.startswith(f"{q}_")]
    if len(prefixed) == 1:
        return prefixed[0], f"{query} -> {prefixed[0]} (unique prefix)"
    if len(prefixed) > 1:
        return "", f"{query} is ambiguous: {', '.join(sorted(prefixed))}"
    return "", "service not in swarm"

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

        services = env.get("data") or []
        resolved, reason = resolve_swarm_service(
            service_name, [str(s.get("name", "")) for s in services]
        )
        if not resolved:
            return {"found": False, "healthy": False, "detail": reason, "service": ""}

        for svc in services:
            if str(svc.get("name", "")) != resolved:
                continue
            desired = svc.get("replicas_desired") or 0
            actual = svc.get("replicas_actual") or 0
            healthy = desired > 0 and actual >= desired
            detail = f"{actual}/{desired}"
            if reason:
                detail = f"{detail} [{reason}]"
            return {"found": True, "healthy": healthy, "detail": detail, "service": resolved}
        return {"found": False, "healthy": False, "detail": "service not in swarm", "service": ""}

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
        """Force-restart a swarm service, resolving a display name first.

        Resolves independently of `service_health` rather than trusting a name
        threaded through a card: the approval hook can run hours after the card
        was raised, and this must never `--force` a service the caller only
        guessed at. An unresolvable or ambiguous name is a refusal, not an
        attempt (issue #166).
        """
        if self.homelab_connector is None or not service_name:
            return {"ok": False, "detail": "no connector or service name"}
        try:
            listing = await self.homelab_connector.list_services()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"could not list services: {str(exc)[:180]}"}
        if not listing.get("ok"):
            return {"ok": False, "detail": f"could not list services: {str(listing.get('error'))[:180]}"}
        resolved, reason = resolve_swarm_service(
            service_name, [str(s.get("name", "")) for s in (listing.get("data") or [])]
        )
        if not resolved:
            return {"ok": False, "detail": reason}
        try:
            env = await self.homelab_connector.restart_service(resolved)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": str(exc)[:200]}
        if not env.get("ok"):
            return {"ok": False, "detail": str(env.get("error"))[:500]}
        detail = str(env.get("data") or "")[:400]
        return {"ok": True, "detail": f"{detail} [{reason}]" if reason else detail,
                "service": resolved}
