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
    assert result == {
        "found": True,
        "healthy": True,
        "detail": "1/1",
        "service": "redis_redis",  # additive: which service we acted on (#166)
    }


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


# --- swarm service-name resolution (issue #166) ---

# The real service list from prod, trimmed to the interesting cases.
_PROD_SERVICES = [
    "aegis_postgres",
    "clickhouse_clickhouse",
    "clickhouse_clickhouse-backup",
    "clickhouse_clickhouse-exporter",
    "loki_loki",
    "loki_promtail",
    "postgresql_postgres",
    "postgresql_postgres-backup",
    "ollama_ollama",
]


def test_resolve_exact_match_wins():
    from aegis_worker.activities.infra_ops import resolve_swarm_service

    name, reason = resolve_swarm_service("clickhouse_clickhouse", _PROD_SERVICES)
    assert name == "clickhouse_clickhouse"
    assert reason == ""


def test_resolve_compose_convention():
    """`Loki is down` names the product; the service is loki_loki."""
    from aegis_worker.activities.infra_ops import resolve_swarm_service

    name, reason = resolve_swarm_service("loki", _PROD_SERVICES)
    assert name == "loki_loki"
    assert "loki_loki" in reason


def test_resolve_unique_prefix():
    """`PostgreSQL is down` -> postgresql_postgres, the only postgresql_* that
    isn't a -backup sidecar... except it isn't unique, so this must refuse."""
    from aegis_worker.activities.infra_ops import resolve_swarm_service

    name, reason = resolve_swarm_service("ollama", _PROD_SERVICES)
    assert name == "ollama_ollama"
    assert reason  # explains the mapping


def test_resolve_convention_beats_ambiguity_for_the_primary_service():
    """`clickhouse` prefix-matches three services, but `clickhouse_clickhouse`
    is the compose convention for the PRIMARY service of that stack — the
    others are -backup/-exporter sidecars. The convention rule is precise, so
    it fires before the ambiguity refusal."""
    from aegis_worker.activities.infra_ops import resolve_swarm_service

    name, reason = resolve_swarm_service("clickhouse", _PROD_SERVICES)
    assert name == "clickhouse_clickhouse"
    assert "convention" in reason


def test_resolve_postgresql_is_ambiguous_in_prod():
    """THE load-bearing refusal. There is no `postgresql_postgresql`, so the
    convention rule misses and BOTH postgresql_postgres and
    postgresql_postgres-backup prefix-match. Force-restarting the wrong one is
    worse than doing nothing (#158's rule), so this refuses rather than
    picking. `PostgreSQL is down` therefore still parks — correctly."""
    from aegis_worker.activities.infra_ops import resolve_swarm_service

    name, reason = resolve_swarm_service("postgresql", _PROD_SERVICES)
    assert name == ""
    assert "ambiguous" in reason


def test_resolve_unknown_service():
    from aegis_worker.activities.infra_ops import resolve_swarm_service

    name, reason = resolve_swarm_service("nosuchthing", _PROD_SERVICES)
    assert name == ""
    assert reason == "service not in swarm"


def test_resolve_empty_query():
    from aegis_worker.activities.infra_ops import resolve_swarm_service

    assert resolve_swarm_service("", _PROD_SERVICES) == ("", "no service name")


async def test_service_health_resolves_a_display_name():
    """End-to-end: `loki` must find loki_loki and report which it acted on."""
    conn = _Connector({"loki_loki": (1, 1), "loki_promtail": (1, 1)})
    result = await InfraOpsActivities(homelab_connector=conn).service_health("loki")
    assert result["found"] is True
    assert result["healthy"] is True
    assert result["service"] == "loki_loki"


async def test_restart_service_refuses_an_ambiguous_name():
    """A refusal, not an attempt — must never --force a guessed service."""
    conn = _Connector({"postgresql_postgres": (1, 1), "postgresql_postgres-backup": (1, 1)})
    result = await InfraOpsActivities(homelab_connector=conn).restart_service("postgresql")
    assert result["ok"] is False
    assert "ambiguous" in result["detail"]
    assert conn.restarted == [], "must not have restarted anything"
