"""Tests for TodoistConnector — Sync API v9 client."""

import respx
from aegis.connectors.todoist import TodoistConnector
from httpx import Response

BASE = "https://api.todoist.com"


@respx.mock
async def test_sync_full_first_time():
    """sync('*') returns full project/task/label snapshot + new sync_token."""
    respx.post(f"{BASE}/api/v1/sync").mock(
        return_value=Response(
            200,
            json={
                "sync_token": "abc123",
                "full_sync": True,
                "items": [
                    {"id": 100, "project_id": 200, "content": "hi", "labels": ["@me"]}
                ],
                "projects": [{"id": 200, "name": "Inbox", "parent_id": None}],
                "labels": [{"id": 300, "name": "@me", "color": "berry_red"}],
            },
        )
    )
    c = TodoistConnector(api_key="test-key")
    result = await c.sync("*", resource_types=["items", "projects", "labels"])
    assert result["ok"] is True
    assert result["data"]["sync_token"] == "abc123"
    assert result["data"]["full_sync"] is True
    assert len(result["data"]["items"]) == 1
    assert len(result["data"]["projects"]) == 1
    assert len(result["data"]["labels"]) == 1


@respx.mock
async def test_sync_incremental_uses_existing_token():
    """sync(token) sends the token in the request body."""
    route = respx.post(f"{BASE}/api/v1/sync").mock(
        return_value=Response(200, json={"sync_token": "next", "items": [], "projects": [], "labels": []})
    )
    c = TodoistConnector(api_key="test-key")
    await c.sync("prev-token", resource_types=["items"])
    request = route.calls[0].request
    body = request.content.decode()
    assert "prev-token" in body


@respx.mock
async def test_sync_unauthorized_returns_envelope_error():
    """401 yields ok=False, error='unauthorized', retryable=False."""
    respx.post(f"{BASE}/api/v1/sync").mock(return_value=Response(401, text="Unauthorized"))
    c = TodoistConnector(api_key="bad-key")
    result = await c.sync("*", resource_types=["items"])
    assert result["ok"] is False
    assert result["error"] == "unauthorized"
    assert result["retryable"] is False


@respx.mock
async def test_sync_5xx_returns_retryable():
    """5xx yields retryable=True; caller will retry next loop."""
    respx.post(f"{BASE}/api/v1/sync").mock(return_value=Response(503, text="overloaded"))
    c = TodoistConnector(api_key="test-key")
    result = await c.sync("*", resource_types=["items"])
    assert result["ok"] is False
    assert result["error"] == "http_503"
    assert result["retryable"] is True


async def test_missing_api_key_short_circuits():
    """No api_key → ok=False, error='no_api_key' before any HTTP call."""
    c = TodoistConnector(api_key="")
    result = await c.sync("*", resource_types=["items"])
    assert result["ok"] is False
    assert result["error"] == "no_api_key"
    assert result["retryable"] is False


@respx.mock
async def test_sync_429_returns_retryable_rate_limited():
    """429 yields ok=False, error='rate_limited', retryable=True."""
    respx.post(f"{BASE}/api/v1/sync").mock(return_value=Response(429, text="rate limit"))
    c = TodoistConnector(api_key="test-key")
    result = await c.sync("*", resource_types=["items"])
    assert result["ok"] is False
    assert result["error"] == "rate_limited"
    assert result["retryable"] is True


@respx.mock
async def test_commands_happy_path():
    """commands(batch) returns ok=True with sync_status + temp_id_mapping."""
    respx.post(f"{BASE}/api/v1/sync").mock(
        return_value=Response(
            200,
            json={
                "sync_status": {"uuid-1": "ok"},
                "temp_id_mapping": {"temp-1": 12345},
            },
        )
    )
    c = TodoistConnector(api_key="test-key")
    result = await c.commands([
        {"type": "item_add", "uuid": "uuid-1", "temp_id": "temp-1", "args": {"content": "x"}}
    ])
    assert result["ok"] is True
    assert result["data"]["sync_status"] == {"uuid-1": "ok"}
    assert result["data"]["temp_id_mapping"] == {"temp-1": 12345}


async def test_commands_empty_short_circuits():
    """An empty commands list short-circuits to ok=True without hitting the network."""
    c = TodoistConnector(api_key="test-key")
    result = await c.commands([])
    assert result["ok"] is True
    assert result["data"] == {"sync_status": {}, "temp_id_mapping": {}}


# --- Phase 3 builders ---

def test_build_note_add_command_shape() -> None:
    cmd = TodoistConnector.build_note_add_command("ITEM123", "hello")
    assert cmd["type"] == "note_add"
    assert cmd["args"] == {"item_id": "ITEM123", "content": "hello"}
    assert cmd["temp_id"].startswith("note-")
    # uuid present and parseable
    import uuid as _uuid
    _uuid.UUID(cmd["uuid"])
    _uuid.UUID(cmd["temp_id"].removeprefix("note-"))


def test_build_note_add_command_with_attachment() -> None:
    attachment = {
        "file_url": "https://files.todoist.com/abc.gz",
        "file_name": "kimi-run.log.gz",
        "file_size": 1024,
        "file_type": "application/gzip",
        "upload_state": "completed",
    }
    cmd = TodoistConnector.build_note_add_command("ITEM123", "see attached", attachment)
    assert cmd["args"]["content"] == "see attached"
    assert cmd["args"]["file_attachment"] == attachment


def test_build_note_add_command_omits_attachment_when_none() -> None:
    cmd = TodoistConnector.build_note_add_command("ITEM123", "hi", None)
    assert "file_attachment" not in cmd["args"]


@respx.mock
async def test_upload_file_happy_path():
    """upload_file POSTs multipart and returns the file_attachment blob."""
    blob = {
        "file_url": "https://files.todoist.com/abc.gz",
        "file_name": "kimi-run.log.gz",
        "file_size": 4096,
        "file_type": "application/gzip",
        "upload_state": "completed",
        "resource_type": "file",
    }
    route = respx.post(f"{BASE}/api/v1/uploads").mock(return_value=Response(200, json=blob))
    c = TodoistConnector(api_key="test-key")
    result = await c.upload_file(
        filename="kimi-run.log.gz",
        content=b"compressed-bytes-here",
        content_type="application/gzip",
    )
    assert result["ok"] is True
    assert result["data"] == blob
    # Verify it was a multipart request (boundary in Content-Type)
    request = route.calls[0].request
    assert "multipart/form-data" in request.headers["content-type"]


@respx.mock
async def test_upload_file_unauthorized():
    respx.post(f"{BASE}/api/v1/uploads").mock(return_value=Response(401, text="bad token"))
    c = TodoistConnector(api_key="bad")
    result = await c.upload_file("x.gz", b"data")
    assert result["ok"] is False
    assert result["error"] == "unauthorized"
    assert result["retryable"] is False


@respx.mock
async def test_upload_file_5xx_retryable():
    respx.post(f"{BASE}/api/v1/uploads").mock(return_value=Response(503, text="overloaded"))
    c = TodoistConnector(api_key="t")
    result = await c.upload_file("x.gz", b"data")
    assert result["ok"] is False
    assert result["error"] == "http_503"
    assert result["retryable"] is True


async def test_upload_file_missing_args():
    c = TodoistConnector(api_key="t")
    assert (await c.upload_file("", b"data"))["error"] == "missing_file"
    assert (await c.upload_file("x", b""))["error"] == "missing_file"


async def test_upload_file_no_api_key():
    c = TodoistConnector(api_key="")
    result = await c.upload_file("x.gz", b"data")
    assert result["ok"] is False
    assert result["error"] == "no_api_key"


# build_item_move_command was removed — GTD filing is label-only, AEGIS never
# relocates tasks between projects (users file into projects manually).


def test_build_item_update_command_passthrough() -> None:
    cmd = TodoistConnector.build_item_update_command(
        "ITEM123",
        labels=["@me", "@deep"],
        priority=3,
    )
    assert cmd["type"] == "item_update"
    assert cmd["args"]["id"] == "ITEM123"
    assert cmd["args"]["labels"] == ["@me", "@deep"]
    assert cmd["args"]["priority"] == 3
    assert "temp_id" not in cmd


def test_is_permanent_error_classification() -> None:
    """Pin the shared permanent-vs-retryable rule used by both
    check_sync_status and drain_outbox."""
    permanent = TodoistConnector._is_permanent_error
    # Tagged 4xx-class rejections are permanent.
    assert permanent({"error_tag": "ITEM_NOT_FOUND"}) is True
    assert permanent({"error_tag": "INVALID_ARGUMENT"}) is True
    # http_code in 400-499 is permanent regardless of tag.
    assert permanent({"http_code": 404}) is True
    assert permanent({"error_tag": "SOMETHING_ELSE", "http_code": 422}) is True
    # 5xx-class is transient.
    assert permanent({"http_code": 500}) is False
    assert permanent({"error_tag": "SOMETHING_ELSE", "http_code": 503}) is False
    # Unknown error dict with no tag/code is transient.
    assert permanent({"error": "boom"}) is False
    # Missing ack (None) is permanent — avoid infinite re-queue.
    assert permanent(None) is True
    # "ok" / other non-dict scalars are not permanent errors.
    assert permanent("ok") is False


def test_check_sync_status_classifies_rejections() -> None:
    """check_sync_status flags permanent rejections as non-retryable and
    pure 5xx rejections as retryable."""
    perm = TodoistConnector.check_sync_status(
        {"ok": True, "data": {"sync_status": {"u1": {"error_tag": "ITEM_NOT_FOUND"}}}},
        ["u1"],
    )
    assert perm["ok"] is False
    assert "u1" in perm["rejected"]
    assert perm["rejected_retryable"] is False

    transient = TodoistConnector.check_sync_status(
        {"ok": True, "data": {"sync_status": {"u1": {"http_code": 503}}}},
        ["u1"],
    )
    assert transient["ok"] is False
    assert transient["rejected_retryable"] is True
