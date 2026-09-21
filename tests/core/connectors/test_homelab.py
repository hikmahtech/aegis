# tests/core/connectors/test_homelab.py
import json
from unittest.mock import AsyncMock, patch

import pytest
from aegis.connectors.homelab import HomelabConnector


@pytest.mark.asyncio
async def test_list_services_returns_envelope():
    conn = HomelabConnector(docker_context="swarm")
    fake_stdout = (
        json.dumps(
            {
                "ID": "s1",
                "Name": "aegis_core",
                "Mode": "replicated",
                "Replicas": "0/1",
                "Image": "youruser/aegis-core:abc123",
            }
        )
        + "\n"
    )
    with patch("aegis.connectors.homelab.asyncio.create_subprocess_exec") as m:
        proc = AsyncMock()
        proc.communicate.return_value = (fake_stdout.encode(), b"")
        proc.returncode = 0
        m.return_value = proc
        env = await conn.list_services()
    assert env["ok"] is True
    assert env["data"][0]["name"] == "aegis_core"
    assert env["data"][0]["replicas_desired"] == 1
    assert env["data"][0]["replicas_actual"] == 0


@pytest.mark.asyncio
async def test_list_services_failure_retryable():
    conn = HomelabConnector(docker_context="x")
    with patch("aegis.connectors.homelab.asyncio.create_subprocess_exec") as m:
        proc = AsyncMock()
        proc.communicate.return_value = (b"", b"context not found")
        proc.returncode = 1
        m.return_value = proc
        env = await conn.list_services()
    assert env["ok"] is False
    assert env["retryable"] is True


@pytest.mark.asyncio
async def test_tls_probe_parses_expiry():
    conn = HomelabConnector(docker_context="x")
    fake = b"notAfter=Nov 15 12:34:56 2026 GMT\nserial=0123456789ABCDEF\n"
    with patch("aegis.connectors.homelab.asyncio.create_subprocess_exec") as m:
        proc = AsyncMock()
        proc.communicate.return_value = (fake, b"")
        proc.returncode = 0
        m.return_value = proc
        env = await conn.probe_tls("example.com")
    assert env["ok"] is True
    assert env["data"]["serial"] == "0123456789ABCDEF"
    assert env["data"]["not_after"].year == 2026


@pytest.mark.asyncio
async def test_list_nodes_returns_envelope(monkeypatch):
    conn = HomelabConnector(docker_context="")
    lines = (
        '{"Hostname": "baa", "Status": "Ready", "Availability": "Active", "ManagerStatus": "Leader"}\n'
        '{"Hostname": "noon", "Status": "Down", "Availability": "Active", "ManagerStatus": ""}\n'
    )

    async def fake_docker(*args, timeout=30):
        assert args == ("node", "ls", "--format", "{{json .}}")
        return (0, lines, "")

    monkeypatch.setattr(conn, "_docker", fake_docker)
    env = await conn.list_nodes()
    assert env["ok"] is True
    assert env["data"] == [
        {"hostname": "baa", "status": "Ready", "availability": "Active", "manager": "Leader"},
        {"hostname": "noon", "status": "Down", "availability": "Active", "manager": ""},
    ]


@pytest.mark.asyncio
async def test_list_nodes_failure_retryable(monkeypatch):
    conn = HomelabConnector(docker_context="")

    async def fake_docker(*args, timeout=30):
        return (1, "", "cannot connect")

    monkeypatch.setattr(conn, "_docker", fake_docker)
    env = await conn.list_nodes()
    assert env["ok"] is False
    assert env["retryable"] is True


@pytest.mark.asyncio
async def test_node_ps_asks_the_managers_about_one_node(monkeypatch):
    """#633: the heartbeat asks which tasks the swarm placed on a node that
    went down, in the JSON shape `docker node ps` prints."""
    conn = HomelabConnector(docker_context="swarm")
    seen: list[tuple] = []

    async def fake_docker(*args, timeout=30):
        seen.append(args)
        rows = [
            {"ID": "t1", "Name": "postgres_postgres.1", "Node": "lam", "DesiredState": "Shutdown",
             "CurrentState": "Running 3 hours ago"},
            {"ID": "t2", "Name": "promtail_promtail.x9z", "Node": "lam", "DesiredState": "Shutdown",
             "CurrentState": "Running 3 hours ago"},
        ]
        return (0, "\n".join(json.dumps(r) for r in rows) + "\nnot json\n", "")

    monkeypatch.setattr(conn, "_docker", fake_docker)
    env = await conn.node_ps("lam")
    assert seen == [("node", "ps", "lam", "--format", "{{json .}}")]
    assert env["ok"] is True
    assert env["data"][0] == {
        "name": "postgres_postgres.1",
        "current_state": "Running 3 hours ago",
        "desired_state": "Shutdown",
    }
    assert len(env["data"]) == 2

    async def failing(*args, timeout=30):
        return (1, "", "node lam not found")

    monkeypatch.setattr(conn, "_docker", failing)
    env = await conn.node_ps("lam")
    assert env["ok"] is False and env["retryable"] is True
