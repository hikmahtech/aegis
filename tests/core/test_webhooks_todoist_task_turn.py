"""Todoist note webhook → immediate task turn for a task that has a session.

A `@code` task owns a `task_sessions` row, and every user comment on it is one
turn of that task's `AgentTaskFlow`. The sweep (`find_turns_due`) would get
there eventually; this fast path gets there in ~1s.

Three things these tests pin, all of which have a failure mode that is invisible
from the HTTP response alone:

* Only a task WITH a session row dispatches a turn — otherwise every comment in
  the account would start an `AgentTaskFlow`.
* Only a USER note dispatches one. A note carrying AEGIS's own `Workflow run:`
  footer reaches this branch (`is_clarify_own` does not catch it) and must be
  filtered by `is_user_note`, or the task answers itself forever.
* The dispatch is best-effort. A Temporal failure must not fail the webhook or
  swallow the ClarifyFlow kick that follows it — Todoist retries a non-200, and
  the sweep is the real backstop for a missed turn.
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

_ITEM = "6CrfhM6VCqCcQXPv"

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


def _mock_pool(executed: list[tuple], session_row: dict | None):
    """Pool recording conn.execute, and answering the session lookup.

    `pool.fetchrow` is the session lookup (`SELECT agent_id FROM task_sessions`)
    and `pool.fetch` is `resolve_tag("gtd")` — the webhook calls one on the pool
    directly and the other through `acquire()`, so both have to exist.
    """
    conn = AsyncMock()

    async def _execute(sql, *args):
        executed.append((sql, args))

    conn.execute = _execute

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    pool.fetch = AsyncMock(return_value=[{"id": "sebas"}])
    pool.fetchrow = AsyncMock(return_value=session_row)
    return pool


@pytest.fixture
def settings():
    return Settings(**_TEST_REQUIRED_SETTINGS)


def _make_client(settings, monkeypatch, *, session_row, fail_workflow=""):
    """(httpx client cm, executed log, temporal start calls, signals).

    `fail_workflow` makes `start_workflow` raise for that workflow name only, so
    a test can break the turn dispatch without breaking the ClarifyFlow kick.
    """
    executed: list[tuple] = []
    temporal_starts: list[dict] = []
    signals: list[tuple] = []

    fake_client = MagicMock()

    async def _start_workflow(*args, **kwargs):
        if fail_workflow and args and args[0] == fail_workflow:
            raise RuntimeError("temporal is having a day")
        temporal_starts.append({"args": args, "kwargs": kwargs})
        return MagicMock()

    async def _signal(name, arg):
        signals.append((name, arg))

    def _get_workflow_handle(wf_id):
        handle = MagicMock()
        handle.signal = _signal
        return handle

    fake_client.start_workflow = _start_workflow
    fake_client.get_workflow_handle = _get_workflow_handle

    class _StubTemporalClient:
        @staticmethod
        async def connect(host):
            return fake_client

    monkeypatch.setattr("temporalio.client.Client", _StubTemporalClient)

    app = create_app(run_lifespan=False)
    app.state.db_pool = _mock_pool(executed, session_row)
    app.dependency_overrides[get_settings] = lambda: settings
    return (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test"),
        executed,
        temporal_starts,
        signals,
    )


async def _post_note(client, content: str, item_id: str = _ITEM):
    body = json.dumps(
        {"event_name": "note:added", "event_data": {"item_id": item_id, "content": content}}
    ).encode()
    return await client.post(
        "/api/webhooks/todoist",
        content=body,
        headers={
            "X-Todoist-Hmac-Sha256": _signed(body, "test-todoist-secret"),
            "Content-Type": "application/json",
        },
    )


def _started(temporal_starts: list[dict], name: str) -> list[dict]:
    return [s for s in temporal_starts if s["args"] and s["args"][0] == name]


@pytest_asyncio.fixture(loop_scope="function")
async def with_session(settings, monkeypatch):
    cm, executed, starts, signals = _make_client(
        settings, monkeypatch, session_row={"agent_id": "pandoras-actor"}
    )
    async with cm as c:
        yield c, executed, starts, signals


@pytest_asyncio.fixture(loop_scope="function")
async def without_session(settings, monkeypatch):
    cm, executed, starts, signals = _make_client(settings, monkeypatch, session_row=None)
    async with cm as c:
        yield c, executed, starts, signals


@pytest_asyncio.fixture(loop_scope="function")
async def dispatch_broken(settings, monkeypatch):
    cm, executed, starts, signals = _make_client(
        settings,
        monkeypatch,
        session_row={"agent_id": "pandoras-actor"},
        fail_workflow="AgentTaskFlow",
    )
    async with cm as c:
        yield c, executed, starts, signals


async def test_user_comment_on_a_session_task_dispatches_a_turn(with_session):
    """The comment lands on the task's single workflow, id derived from the task
    so a second comment reaches the same run — and ClarifyFlow still runs, because
    the two paths answer different questions about the same note."""
    client, _executed, starts, _signals = with_session
    r = await _post_note(client, "use the other repo")
    assert r.status_code == 200, r.text

    turns = _started(starts, "AgentTaskFlow")
    assert len(turns) == 1
    payload = turns[0]["args"][1]
    assert turns[0]["kwargs"]["id"] == f"agent-task-{_ITEM}"
    assert turns[0]["kwargs"]["task_queue"] == "aegis-main"
    assert payload["comment"] == "use the other repo"
    assert payload["agent_id"] == "pandoras-actor"
    assert payload["todoist_task_id"] == _ITEM

    assert len(_started(starts, "ClarifyFlow")) == 1


async def test_a_task_without_a_session_dispatches_no_turn(without_session):
    """Without this gate every comment in the account would start a coding run."""
    client, _executed, starts, _signals = without_session
    r = await _post_note(client, "just a normal comment")
    assert r.status_code == 200, r.text
    assert _started(starts, "AgentTaskFlow") == []
    assert len(_started(starts, "ClarifyFlow")) == 1


async def test_an_agent_reply_note_starts_nothing(with_session):
    """`[Agent reply @ ` is AEGIS's own comment. It must reach neither path — a
    turn on it would have the agent answering itself."""
    client, executed, starts, _signals = with_session
    r = await _post_note(client, "[Agent reply @ 12:30 agent=pandora]\nDone.")
    assert r.status_code == 200, r.text
    assert starts == []
    # Audit insert only — no last_note_at bump either.
    assert len(executed) == 1
    assert "INSERT INTO todoist_webhook_events" in executed[0][0]


async def test_a_workflow_run_footer_note_dispatches_no_turn(with_session):
    """The turn's own progress comment carries a `Workflow run:` footer and NOT
    the agent-reply prefix, so it passes the clarify self-loop guard and only
    `is_user_note` stops it. Without that check the session answers itself in a
    loop, each turn posting the note that starts the next one."""
    client, _executed, starts, _signals = with_session
    r = await _post_note(client, "Planned the change.\n\nWorkflow run: agent-task-123")
    assert r.status_code == 200, r.text
    assert _started(starts, "AgentTaskFlow") == []
    assert len(_started(starts, "ClarifyFlow")) == 1


async def test_a_failed_dispatch_neither_fails_the_webhook_nor_eats_clarify(dispatch_broken):
    """Todoist retries a non-200 and the sweep re-picks a missed turn, so a
    Temporal outage must be logged and stepped over — not raised, and not
    allowed to skip the ClarifyFlow kick that comes after it."""
    client, _executed, starts, _signals = dispatch_broken
    with structlog.testing.capture_logs() as log_entries:
        r = await _post_note(client, "use the other repo")
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": True}
    assert _started(starts, "AgentTaskFlow") == []
    assert len(_started(starts, "ClarifyFlow")) == 1
    assert "todoist_webhook_task_turn_failed" in [e.get("event") for e in log_entries]
