"""Webhook receiver must not bump last_note_at OR kick ClarifyFlow when
the incoming note is an AGENT_REPLY_PREFIX comment authored by us.
Without this guard, an agent's reply fires another webhook → next tick
re-fires the agent → infinite loop.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json as _json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from httpx import ASGITransport, AsyncClient

_TEST_REQUIRED_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "n8n_ui_url": "https://n8n.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
    "todoist_webhook_secret": "test-secret",
    "temporal_host": "",  # disable Temporal path for unit test
}


def _signed(body: bytes, secret: str) -> str:
    # Todoist base64-encodes the digest (not hex, unlike GitHub/Sentry).
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def _mock_pool(executed: list[tuple]):
    """Pool that records every conn.execute call into `executed`."""
    conn = AsyncMock()

    async def _execute(sql, *args):
        executed.append((sql, args))

    conn.execute = _execute

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


@pytest.fixture
def settings():
    return Settings(**_TEST_REQUIRED_SETTINGS)


@pytest_asyncio.fixture(loop_scope="function")
async def client_and_state(settings):
    """Returns (httpx client, executed_log)."""
    executed: list[tuple] = []

    app = create_app(run_lifespan=False)
    app.state.db_pool = _mock_pool(executed)
    app.dependency_overrides[get_settings] = lambda: settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, executed


@pytest.mark.asyncio
async def test_webhook_skips_bump_on_agent_reply_prefix(client_and_state):
    """AGENT_REPLY_PREFIX content → no last_note_at bump, no Temporal kick."""
    client, executed = client_and_state

    payload = {
        "event_name": "note:added",
        "event_data": {
            "item_id": "abc123",
            "content": "[Agent reply @ 12:30 agent=raphael]\nHere is what I found...",
        },
    }
    body = _json.dumps(payload).encode()
    sig = _signed(body, "test-secret")

    r = await client.post(
        "/api/webhooks/todoist",
        content=body,
        headers={"X-Todoist-Hmac-Sha256": sig, "Content-Type": "application/json"},
    )

    assert r.status_code == 200

    # Only the audit INSERT should have run — no last_note_at bump
    assert len(executed) == 1
    assert "INSERT INTO todoist_webhook_events" in executed[0][0]

    # Confirm: no UPDATE on todoist_tasks (the loop-triggering action)
    bump_sqls = [sql for sql, _ in executed if "UPDATE todoist_tasks SET last_note_at" in sql]
    assert bump_sqls == [], (
        "Self-loop guard fired: UPDATE todoist_tasks should NOT run for AGENT_REPLY_PREFIX"
    )
