"""InfraOpsActivities — thin activity wrappers over HomelabConnector.

The fake connector below mirrors HomelabConnector's real envelope shape
(`{"ok": bool, "data": ..., "error": ...}` with `replicas_actual`/
`replicas_desired` ints per service — see
core/src/aegis/connectors/homelab.py), not a simplified one, so these tests
would actually catch a mismatch against the real connector.
"""

from __future__ import annotations

from aegis_worker.activities.infra_ops import InfraOpsActivities


class _Connector:
    def __init__(self, services: dict, restart_ok: bool = True):
        # services: {name: (replicas_actual, replicas_desired)}
        self._services = services
        self._restart_ok = restart_ok
        self.restarted: list[str] = []

    async def list_services(self) -> dict:
        return {
            "ok": True,
            "data": [
                {"name": n, "replicas_actual": a, "replicas_desired": d}
                for n, (a, d) in self._services.items()
            ],
            "error": None,
        }

    async def service_ps(self, service_name: str) -> dict:
        return {
            "ok": True,
            "data": [
                {
                    "current_state": "Running",
                    "desired_state": "Running",
                    "error": "",
                    "node": "baa",
                }
            ],
            "error": None,
        }

    async def restart_service(self, service_name: str) -> dict:
        self.restarted.append(service_name)
        return {
            "ok": self._restart_ok,
            "data": {"output": "restarted"} if self._restart_ok else None,
            "error": None if self._restart_ok else "docker service update --force failed",
        }


async def test_service_health_healthy_when_replicas_match():
    act = InfraOpsActivities(homelab_connector=_Connector({"redis_redis": (1, 1)}))
    result = await act.service_health("redis_redis")
    assert result == {"found": True, "healthy": True, "detail": "1/1"}


async def test_service_health_unhealthy_when_replicas_short():
    act = InfraOpsActivities(homelab_connector=_Connector({"redis_redis": (0, 1)}))
    result = await act.service_health("redis_redis")
    assert result["found"] is True
    assert result["healthy"] is False


async def test_service_health_not_found():
    act = InfraOpsActivities(homelab_connector=_Connector({"other_svc": (1, 1)}))
    assert (await act.service_health("redis_redis"))["found"] is False


async def test_service_health_no_connector_is_not_found_not_crash():
    assert (await InfraOpsActivities(homelab_connector=None).service_health("x"))["found"] is False


async def test_service_logs_renders_task_state():
    act = InfraOpsActivities(homelab_connector=_Connector({"redis_redis": (1, 1)}))
    result = await act.service_logs("redis_redis")
    assert "Running" in result["logs"]


async def test_restart_service_delegates():
    conn = _Connector({"redis_redis": (0, 1)})
    assert (await InfraOpsActivities(homelab_connector=conn).restart_service("redis_redis"))["ok"]
    assert conn.restarted == ["redis_redis"]
