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
