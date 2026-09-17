"""Every delivery endpoint takes the same API key, and the two that had no
401 test now have one.

`/api/deliver/message` and `/api/deliver/document` were checking the key with
their own inline copy and nothing asserted it; #603 moved all five onto one
`require_api_key` dependency, which is exactly the change that could silently
open an endpoint by leaving the dependency off one decorator. Health takes no
key at all — it is the swarm's probe.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("AEGIS_API_KEY", "test-key")
    adapter = AsyncMock()
    adapter.name = "slack"

    from aegis_comms.__main__ import create_delivery_app
    from aegis_comms.config import CommsSettings

    return create_delivery_app(adapter, CommsSettings(_env_file=None))


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/deliver/message", {"agent_id": "a", "text": "hi"}),
        (
            "/api/deliver/document",
            {"agent_id": "a", "documents": [{"filename": "x.txt", "content": "y"}]},
        ),
    ],
)
@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong-key"}])
async def test_a_missing_or_wrong_key_is_401(app, path, body, headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(path, json=body, headers=headers)
    assert resp.status_code == 401


async def test_health_needs_no_key(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/health")
    assert resp.status_code == 200
