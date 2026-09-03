"""The two task-thread routes comms calls when a Slack reply lands in a thread.

The load-bearing assertion in this file is that the comment route stores the
text **verbatim**. A note carrying the `Workflow run:` footer (or either agent
prefix) is AEGIS's own comment and `task_sessions.is_user_note` filters it out,
so a route that helpfully prefixed the author would post a note that never
starts a turn — the reply would land in Todoist and nothing would happen.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.connectors.todoist import TodoistConnector
from aegis.services import task_sessions as svc

pytestmark = pytest.mark.asyncio

_TASK = "route-ts-task-1"
_OTHER = "route-ts-task-2"

_TEST_REQUIRED_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "n8n_ui_url": "https://n8n.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
}


@pytest.fixture
def settings():
    return Settings(**_TEST_REQUIRED_SETTINGS)


@pytest_asyncio.fixture(loop_scope="function")
async def app(db_pool, settings):
    """The real Core app, so the test proves the router is registered in app.py."""
    for task in (_TASK, _OTHER):
        await db_pool.execute("DELETE FROM task_sessions WHERE task_id = $1", task)
    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    await svc.set_slack_ref(db_pool, _TASK, {"channel": "C-TASKS", "ts": "1756900000.000100"})

    created = create_app(run_lifespan=False)
    created.state.db_pool = db_pool
    created.dependency_overrides[get_settings] = lambda: settings
    created.dependency_overrides[verify_auth] = lambda: True
    yield created
    for task in (_TASK, _OTHER):
        await db_pool.execute("DELETE FROM task_sessions WHERE task_id = $1", task)


@pytest_asyncio.fixture(loop_scope="function")
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


@pytest.fixture
def sent(monkeypatch):
    """Record every Sync command the route sends; report every one as accepted."""
    batches: list[list[dict]] = []

    async def fake_commands(self, commands):
        batches.append(commands)
        return {
            "ok": True,
            "data": {"sync_status": {c["uuid"]: "ok" for c in commands}},
        }

    monkeypatch.setattr(TodoistConnector, "commands", fake_commands)
    monkeypatch.setattr(
        "aegis.api.routes.task_sessions.resolve_todoist_api_key",
        _fake_key("test-todoist-key"),
    )
    return batches


def _fake_key(key: str):
    async def _resolve(pool, settings):
        return key

    return _resolve


# ---------------------------------------------------------------- by-thread


async def test_by_thread_finds_the_task_owning_the_thread(client):
    r = await client.get(
        "/api/admin/task-sessions/by-thread",
        params={"channel": "C-TASKS", "ts": "1756900000.000100"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"task_id": _TASK}


async def test_by_thread_returns_null_for_an_unknown_thread(client):
    """A miss is 200 + null, not 404: comms falls through to normal routing on
    every Slack thread that is not a task thread, which is most of them."""
    r = await client.get(
        "/api/admin/task-sessions/by-thread",
        params={"channel": "C-TASKS", "ts": "9999999999.999999"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"task_id": None}


async def test_by_thread_does_not_match_the_ts_in_another_channel(client):
    r = await client.get(
        "/api/admin/task-sessions/by-thread",
        params={"channel": "C-OTHER", "ts": "1756900000.000100"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"task_id": None}


# ------------------------------------------------------------------ comment


async def test_comment_posts_the_text_verbatim(client, sent):
    """No prefix, no footer, no wrapper — the note must read as user-authored or
    `find_turns_due` skips it and the reply never starts a turn.

    Falsifiable: prefix the content in the route and this fails on both the
    equality and the `is_user_note` assertion.
    """
    text = "also run the migration on staging"
    r = await client.post(f"/api/admin/tasks/{_TASK}/comment", json={"text": text})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "task_id": _TASK}

    assert len(sent) == 1 and len(sent[0]) == 1
    cmd = sent[0][0]
    assert cmd["type"] == "note_add"
    assert cmd["args"]["item_id"] == _TASK
    assert cmd["args"]["content"] == text
    assert svc.is_user_note(cmd["args"]["content"]), "must count as a USER note"


async def test_comment_404_when_the_task_has_no_session(client, sent):
    """Falsifiable: drop the get_session guard and this returns 200."""
    r = await client.post(f"/api/admin/tasks/{_OTHER}/comment", json={"text": "hello"})
    assert r.status_code == 404, r.text
    assert sent == [], "no Todoist write for a task we do not own a session for"


async def test_comment_400_when_the_text_is_blank(client, sent):
    r = await client.post(f"/api/admin/tasks/{_TASK}/comment", json={"text": "   \n "})
    assert r.status_code == 400, r.text
    assert sent == []


async def test_comment_503_when_todoist_is_not_configured(client, monkeypatch):
    async def _no_commands(self, commands):  # pragma: no cover — must not run
        raise AssertionError("the route wrote to Todoist without a key")

    monkeypatch.setattr(TodoistConnector, "commands", _no_commands)
    monkeypatch.setattr(
        "aegis.api.routes.task_sessions.resolve_todoist_api_key", _fake_key("")
    )
    r = await client.post(f"/api/admin/tasks/{_TASK}/comment", json={"text": "hi"})
    assert r.status_code == 503, r.text


async def test_comment_400s_over_15000_chars_instead_of_truncating(client, sent):
    """Todoist rejects an over-long note outright, so the route refuses first.

    Refusing, not clipping: the reply lands on the task under the user's name,
    and a silently truncated one is a sentence they did not write. The caller
    gets a 400 it can report in the thread. Same rule as the `comment_on_task`
    chat tool, which refuses at the same length.

    Falsifiable: restore `text[:MAX_NOTE_CHARS]` and this returns 200.
    """
    r = await client.post(f"/api/admin/tasks/{_TASK}/comment", json={"text": "x" * 20000})
    assert r.status_code == 400, r.text
    assert "20000" in r.json()["detail"]
    assert sent == [], "nothing reaches Todoist"


async def test_comment_posts_at_exactly_the_cap(client, sent):
    """The boundary is inclusive — 15,000 is what Todoist accepts."""
    r = await client.post(f"/api/admin/tasks/{_TASK}/comment", json={"text": "x" * 15000})
    assert r.status_code == 200, r.text
    assert len(sent[0][0]["args"]["content"]) == 15000


async def test_comment_preserves_leading_and_trailing_whitespace(client, sent):
    """The note is the caller's exact string, not a stripped copy.

    A Slack reply is routinely a fenced code block or an indented diff; the
    layout is content. The stripped copy exists only to reject a blank reply.

    Falsifiable: post `text.strip()` and this fails on the equality.
    """
    text = "\n```diff\n-  old = 1\n+  new = 2\n```\n\n"
    r = await client.post(f"/api/admin/tasks/{_TASK}/comment", json={"text": text})
    assert r.status_code == 200, r.text
    assert sent[0][0]["args"]["content"] == text


async def test_comment_reports_not_ok_when_todoist_rejects_the_note(client, monkeypatch):
    """A per-command rejection inside an ok envelope is the silent-failure shape
    `check_sync_status` exists for: the route must report it, not return ok."""

    async def rejecting(self, commands):
        return {
            "ok": True,
            "data": {
                "sync_status": {
                    c["uuid"]: {"error_tag": "ITEM_NOT_FOUND", "error": "gone"}
                    for c in commands
                }
            },
        }

    monkeypatch.setattr(TodoistConnector, "commands", rejecting)
    monkeypatch.setattr(
        "aegis.api.routes.task_sessions.resolve_todoist_api_key",
        _fake_key("test-todoist-key"),
    )
    r = await client.post(f"/api/admin/tasks/{_TASK}/comment", json={"text": "hi"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": False, "task_id": _TASK}


async def test_comment_requires_a_text_field(client, sent):
    r = await client.post(f"/api/admin/tasks/{_TASK}/comment", json={})
    assert r.status_code == 422, r.text
    assert sent == []


async def test_both_routes_are_mounted_on_the_real_app(app):
    """Registered in app.py, not just importable — the whole point of the pair
    is that comms can reach them.
    """
    # Read the mounted paths off the OpenAPI schema: this FastAPI version wraps
    # each include_router() in an opaque router object, so app.routes does not
    # expose them.
    paths = set(app.openapi()["paths"])
    assert "/api/admin/task-sessions/by-thread" in paths
    assert "/api/admin/tasks/{task_id}/comment" in paths


async def test_by_thread_ignores_a_session_with_no_slack_ref(client, db_pool):
    """A task whose thread root was never delivered has slack_ref NULL; the
    lookup must not match it for an empty channel/ts."""
    await svc.create_session(db_pool, task_id=_OTHER, agent_id="pandoras-actor")
    r = await client.get(
        "/api/admin/task-sessions/by-thread", params={"channel": "", "ts": ""}
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"task_id": None}


async def test_comment_survives_a_task_id_with_odd_characters(client, sent, db_pool):
    """Todoist v1 ids are opaque strings, not digits."""
    task_id = f"6X{uuid.uuid4().hex[:8]}"
    await db_pool.execute("DELETE FROM task_sessions WHERE task_id = $1", task_id)
    await svc.create_session(db_pool, task_id=task_id, agent_id="pandoras-actor")
    try:
        r = await client.post(f"/api/admin/tasks/{task_id}/comment", json={"text": "go"})
        assert r.status_code == 200, r.text
        assert sent[0][0]["args"]["item_id"] == task_id
    finally:
        await db_pool.execute("DELETE FROM task_sessions WHERE task_id = $1", task_id)
