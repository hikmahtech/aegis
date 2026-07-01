"""Phase 5 polish: Todoist webhook note:added → instant-clarify trigger.

Asserts the existing /api/webhooks/todoist endpoint now does TWO things on
a `note:added` event:
1. Bumps todoist_tasks.last_note_at for the item — so the next
   ClarifyFlow tick re-queues the row via the existing find_unclassified
   guard (last_note_at > last_clarified_at).
2. Best-effort starts a ClarifyFlow workflow on the spot for ~1s
   supervision latency instead of waiting up to 15min.

The ClarifyFlow guard against AEGIS-authored notes (content starts
with '[ClarifyFlow @ ') is also exercised: those events MUST NOT bump
last_note_at or trigger a workflow start.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
import structlog.testing
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
    "todoist_webhook_secret": "test-todoist-secret",
    "temporal_host": "fake-temporal:7233",
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
async def client_and_state(settings, monkeypatch):
    """Returns (httpx client, executed_log, temporal_start_calls)."""
    executed: list[tuple] = []
    temporal_starts: list[dict] = []

    fake_client = MagicMock()

    async def _start_workflow(*args, **kwargs):
        temporal_starts.append({"args": args, "kwargs": kwargs})
        return MagicMock()

    fake_client.start_workflow = _start_workflow

    class _StubTemporalClient:
        @staticmethod
        async def connect(host):
            return fake_client

    monkeypatch.setattr("temporalio.client.Client", _StubTemporalClient)

    app = create_app(run_lifespan=False)
    app.state.db_pool = _mock_pool(executed)
    app.dependency_overrides[get_settings] = lambda: settings
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c, executed, temporal_starts


@pytest.mark.asyncio
async def test_todoist_webhook_note_added_bumps_last_note_at(client_and_state):
    """note:added event triggers (a) audit insert, (b) last_note_at bump,
    (c) ClarifyFlow start_workflow."""
    client, executed, temporal_starts = client_and_state
    body_dict = {
        "event_name": "note:added",
        "event_data": {
            "item_id": "6CrfhM6VCqCcQXPv",
            "content": "user wrote this comment",
        },
    }
    body = json.dumps(body_dict).encode()
    sig = _signed(body, "test-todoist-secret")
    r = await client.post(
        "/api/webhooks/todoist",
        content=body,
        headers={"X-Todoist-Hmac-Sha256": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": True}

    # Audit insert (1) + last_note_at bump (2)
    assert len(executed) == 2
    audit_sql = executed[0][0]
    bump_sql = executed[1][0]
    assert "INSERT INTO todoist_webhook_events" in audit_sql
    assert "UPDATE todoist_tasks SET last_note_at" in bump_sql
    assert executed[1][1] == ("6CrfhM6VCqCcQXPv",)

    # ClarifyFlow was kicked off
    assert len(temporal_starts) == 1
    assert temporal_starts[0]["args"][0] == "ClarifyFlow"
    assert temporal_starts[0]["kwargs"]["task_queue"] == "aegis-main"


@pytest.mark.asyncio
async def test_todoist_webhook_note_bump_success_log_does_not_raise(client_and_state):
    """Regression guard: the success-path log call after a successful
    last_note_at bump must not itself raise. structlog's default bound
    logger (active here — this codebase never calls structlog.configure())
    reserves `event` as the first positional arg; passing `event=...` as a
    kwarg too raises `TypeError: ...meth() got multiple values for argument
    'event'`, which the surrounding try/except then silently mislabels as
    `todoist_webhook_note_bump_failed` even though the DB write succeeded.
    Caught via structlog.testing.capture_logs — the prior assertions on
    `executed` can't see this because they only check DB side effects, not
    whether the success log itself blew up.
    """
    client, executed, temporal_starts = client_and_state
    body_dict = {
        "event_name": "note:added",
        "event_data": {"item_id": "6CrfhM6VCqCcQXPv", "content": "another comment"},
    }
    body = json.dumps(body_dict).encode()
    sig = _signed(body, "test-todoist-secret")
    with structlog.testing.capture_logs() as log_entries:
        r = await client.post(
            "/api/webhooks/todoist",
            content=body,
            headers={"X-Todoist-Hmac-Sha256": sig, "Content-Type": "application/json"},
        )
    assert r.status_code == 200, r.text
    events = [e.get("event") for e in log_entries]
    assert "todoist_webhook_note_bumped_last_note_at" in events
    assert "todoist_webhook_note_bump_failed" not in events


@pytest.mark.asyncio
async def test_todoist_webhook_clarify_own_note_does_not_bump(client_and_state):
    """Comments that start with '[ClarifyFlow @ ' are AEGIS-authored and
    MUST NOT trigger last_note_at bump (else infinite loop)."""
    client, executed, temporal_starts = client_and_state
    body_dict = {
        "event_name": "note:added",
        "event_data": {
            "item_id": "6CrfhM6VCqCcQXPv",
            "content": "[ClarifyFlow @ 14:30 UTC · pass 1] @me · trash",
        },
    }
    body = json.dumps(body_dict).encode()
    sig = _signed(body, "test-todoist-secret")
    r = await client.post(
        "/api/webhooks/todoist",
        content=body,
        headers={"X-Todoist-Hmac-Sha256": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    # Only the audit insert happened — no last_note_at bump, no clarify start
    assert len(executed) == 1
    assert "INSERT INTO todoist_webhook_events" in executed[0][0]
    assert len(temporal_starts) == 0


@pytest.mark.asyncio
async def test_todoist_webhook_other_events_unchanged(client_and_state):
    """item:added / item:updated etc. do NOT bump last_note_at."""
    client, executed, temporal_starts = client_and_state
    body_dict = {
        "event_name": "item:added",
        "event_data": {"id": "x"},
    }
    body = json.dumps(body_dict).encode()
    sig = _signed(body, "test-todoist-secret")
    r = await client.post(
        "/api/webhooks/todoist",
        content=body,
        headers={"X-Todoist-Hmac-Sha256": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert len(executed) == 1  # audit only
    assert len(temporal_starts) == 0


@pytest.mark.asyncio
async def test_todoist_webhook_bad_signature_rejected(client_and_state):
    client, executed, temporal_starts = client_and_state
    body = json.dumps({"event_name": "note:added", "event_data": {"item_id": "x"}}).encode()
    r = await client.post(
        "/api/webhooks/todoist",
        content=body,
        headers={"X-Todoist-Hmac-Sha256": "wrong"},
    )
    assert r.status_code == 401
