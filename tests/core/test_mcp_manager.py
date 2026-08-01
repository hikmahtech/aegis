"""Failure-isolation tests for the MCP streamable-HTTP client.

An MCP server is an external, untrusted party. These tests pin the properties
that keep a hostile or broken one from taking Core down: fail-closed when the
subsystem is off, bounded time, bounded bytes, typed errors on garbage, and no
credential in any log line or error string.

No test here opens a real socket — every server is a ``respx`` mock.
"""

import asyncio
import json
import time

import httpx
import pytest
import respx
import structlog
from aegis.mcp_manager import (
    MCPConfigError,
    MCPDisabledError,
    MCPManager,
    MCPProtocolError,
    MCPResponseTooLargeError,
    MCPServerError,
    MCPTimeoutError,
    MCPTransportError,
    MCPUnknownServerError,
    parse_server_config,
)

URL = "https://zzb8-mcp.invalid/mcp"

_INIT_RESULT = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "serverInfo": {"name": "zzb8-echo", "version": "1"},
}


def _fake_server(*, tools=None, call_result=None, on_call=None):
    """A respx side-effect implementing the initialize/list/call handshake.

    ``on_call`` (if given) handles ``tools/call`` so a test can inject garbage
    without re-implementing the handshake.
    """
    tools = (
        tools if tools is not None else [{"name": "echo", "description": "Echo", "inputSchema": {}}]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = payload.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": payload["id"], "result": _INIT_RESULT},
                headers={"Mcp-Session-Id": "zzb8-session"},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": tools}}
            )
        if method == "tools/call":
            if on_call is not None:
                return on_call(payload)
            result = call_result if call_result is not None else {"content": [], "isError": False}
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result}
            )
        return httpx.Response(400, text="unexpected method")

    return handler


# --------------------------------------------------------------------------
# Fail closed
# --------------------------------------------------------------------------


async def test_disabled_manager_opens_no_connection():
    """Flag off ⇒ typed refusal and NOT ONE request, even with servers configured."""
    async with respx.mock(assert_all_called=False) as router:
        route = router.post(URL).mock(side_effect=_fake_server())
        manager = MCPManager({"echo": {"url": URL}}, enabled=False)

        assert manager.enabled is False
        with pytest.raises(MCPDisabledError):
            await manager.call_tool("echo", "echo", {})
        with pytest.raises(MCPDisabledError):
            await manager.list_tools("echo")

        assert route.call_count == 0, "disabled manager must never contact a server"


async def test_no_servers_configured_is_an_explicit_unknown_server():
    """Nothing configured ⇒ a typed error naming the server, never None."""
    manager = MCPManager({}, enabled=True)
    assert manager.list_servers() == []
    with pytest.raises(MCPUnknownServerError, match="zzb8-nope"):
        await manager.call_tool("zzb8-nope", "t", {})


async def test_blank_auth_token_is_rejected_not_downgraded_to_anonymous():
    """A declared-but-empty credential must not become an anonymous connect."""
    async with respx.mock(assert_all_called=False) as router:
        route = router.post(URL).mock(side_effect=_fake_server())
        manager = MCPManager({"echo": {"url": URL, "auth_token": "  "}}, enabled=True)

        assert manager.list_servers()[0]["usable"] is False
        with pytest.raises(MCPConfigError, match="refusing to connect anonymously"):
            await manager.call_tool("echo", "echo", {})
        assert route.call_count == 0


async def test_stdio_transport_is_rejected_with_an_explicit_reason():
    """stdio would spawn a local process — refuse loudly, never silently ignore."""
    with pytest.raises(MCPConfigError, match="stdio transport is not supported"):
        parse_server_config("x", {"transport": "stdio", "command": ["/bin/sh"]})


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("not-a-dict", "must be an object"),
        ({"transport": "carrier-pigeon", "url": URL}, "unknown transport"),
        ({}, "'url' is required"),
        ({"url": "/relative/path"}, "absolute http"),
        ({"url": URL, "timeout_s": "soon"}, "must be a number"),
        ({"url": URL, "timeout_s": 9999}, "must be in"),
        ({"url": URL, "max_response_bytes": 0}, "must be in"),
    ],
)
def test_malformed_server_entry_raises_config_error(raw, expected):
    with pytest.raises(MCPConfigError, match=expected):
        parse_server_config("zzb8-bad", raw)


async def test_bad_entry_is_observable_and_does_not_kill_the_good_one():
    """Issue #205: a broken entry logs at ERROR and fails fast — no silent None."""
    with structlog.testing.capture_logs() as logs:
        manager = MCPManager(
            {"good": {"url": URL}, "zzb8-broken": {"transport": "stdio", "command": "x"}},
            enabled=True,
        )

    rejected = [e for e in logs if e["event"] == "mcp_server_config_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["log_level"] == "error"
    assert rejected[0]["server"] == "zzb8-broken"

    rows = {row["name"]: row for row in manager.list_servers()}
    assert rows["good"]["usable"] is True
    assert rows["zzb8-broken"]["usable"] is False
    assert "stdio" in rows["zzb8-broken"]["error"]

    async with respx.mock(assert_all_called=False) as router:
        router.post(URL).mock(side_effect=_fake_server())
        with pytest.raises(MCPConfigError):
            await manager.call_tool("zzb8-broken", "t", {})
        # The healthy server still works — one bad entry wedges nothing.
        assert await manager.call_tool("good", "echo", {}) == {"content": [], "isError": False}
        await manager.close()


# --------------------------------------------------------------------------
# Happy path + discovery
# --------------------------------------------------------------------------


async def test_handshake_then_list_and_call():
    async with respx.mock(assert_all_called=False) as router:
        router.post(URL).mock(
            side_effect=_fake_server(
                tools=[
                    {"name": "echo", "description": "Echo back", "inputSchema": {"type": "object"}}
                ],
                call_result={"content": [{"type": "text", "text": "pong"}], "isError": False},
            )
        )
        manager = MCPManager({"echo": {"url": URL}}, enabled=True)

        assert await manager.list_tools("echo") == [
            {"name": "echo", "description": "Echo back", "inputSchema": {"type": "object"}}
        ]
        assert await manager.call_tool("echo", "echo", {"msg": "ping"}) == {
            "content": [{"type": "text", "text": "pong"}],
            "isError": False,
        }

        methods = [json.loads(c.request.content).get("method") for c in router.calls]
        assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
        # The session id the server handed out is echoed back on later requests.
        assert router.calls[-1].request.headers.get("mcp-session-id") == "zzb8-session"
        await manager.close()


async def test_connect_does_not_execute_any_tool():
    """Discovery is not execution: listing tools must never call one (B9's gate)."""
    async with respx.mock(assert_all_called=False) as router:
        router.post(URL).mock(side_effect=_fake_server())
        manager = MCPManager({"echo": {"url": URL}}, enabled=True)

        await manager.list_tools("echo")

        methods = [json.loads(c.request.content).get("method") for c in router.calls]
        assert "tools/call" not in methods
        await manager.close()


async def test_tools_are_cached_across_calls():
    async with respx.mock(assert_all_called=False) as router:
        router.post(URL).mock(side_effect=_fake_server())
        manager = MCPManager({"echo": {"url": URL}}, enabled=True)

        await manager.list_tools("echo")
        await manager.list_tools("echo")

        methods = [json.loads(c.request.content).get("method") for c in router.calls]
        assert methods.count("tools/list") == 1
        await manager.close()


async def test_sse_framed_response_is_parsed():
    """Streamable HTTP may answer with text/event-stream instead of JSON."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload.get("method") == "notifications/initialized":
            return httpx.Response(202)
        result = _INIT_RESULT if payload["method"] == "initialize" else {"content": ["sse"]}
        frame = "event: message\ndata: " + json.dumps(
            {"jsonrpc": "2.0", "id": payload["id"], "result": result}
        )
        return httpx.Response(
            200, content=frame + "\n\n", headers={"content-type": "text/event-stream"}
        )

    async with respx.mock(assert_all_called=False) as router:
        router.post(URL).mock(side_effect=handler)
        manager = MCPManager({"echo": {"url": URL}}, enabled=True)
        assert await manager.call_tool("echo", "echo", {}) == {"content": ["sse"]}
        await manager.close()


# --------------------------------------------------------------------------
# Bounded time
# --------------------------------------------------------------------------


async def test_a_hanging_server_is_bounded_by_the_total_call_budget():
    """A server that never answers must not hold a request handler open."""

    async def never_answers(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, json={})

    async with respx.mock(assert_all_called=False) as router:
        router.post(URL).mock(side_effect=never_answers)
        manager = MCPManager({"slow": {"url": URL, "timeout_s": 0.25}}, enabled=True)

        started = time.monotonic()
        with pytest.raises(MCPTimeoutError, match="budget"):
            await manager.call_tool("slow", "echo", {})
        elapsed = time.monotonic() - started

        assert elapsed < 5, f"call was not bounded: took {elapsed:.1f}s"
        await manager.close()


async def test_slow_handshake_is_bounded_by_the_total_budget_not_just_per_request():
    """Each round trip beats the read timeout, but the sequence must not.

    Proves the *total* call bound exists: initialize + initialized + tools/call
    at 0.35s each is over 1s, while no single request ever hits the 0.9s read
    timeout, so only the total-budget guard can stop this.
    """

    async def sluggish(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.35)
        return _fake_server()(request)

    async with respx.mock(assert_all_called=False) as router:
        router.post(URL).mock(side_effect=sluggish)
        manager = MCPManager({"slow": {"url": URL, "timeout_s": 0.9}}, enabled=True)

        started = time.monotonic()
        with pytest.raises(MCPTimeoutError, match="budget"):
            await manager.call_tool("slow", "echo", {})
        assert time.monotonic() - started < 2
        await manager.close()


async def test_transport_timeout_maps_to_a_typed_error():
    async with respx.mock(assert_all_called=False) as router:
        router.post(URL).mock(side_effect=httpx.ConnectTimeout("connect timed out"))
        manager = MCPManager({"slow": {"url": URL}}, enabled=True)
        with pytest.raises(MCPTimeoutError):
            await manager.call_tool("slow", "echo", {})
        await manager.close()


async def test_a_failed_call_does_not_wedge_an_established_session():
    """A restarted server must cost ONE failure, not every call from then on.

    The session is established first, then the server starts rejecting the
    stale ``Mcp-Session-Id`` with a 400 (not the 404 the transport already
    special-cases). Only resetting the session on failure lets the very next
    call re-initialise and succeed.
    """
    state = {"valid": None, "minted": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload.get("method") == "initialize":
            state["minted"] += 1
            state["valid"] = f"zzb8-session-{state['minted']}"
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": payload["id"], "result": _INIT_RESULT},
                headers={"Mcp-Session-Id": state["valid"]},
            )
        sent = request.headers.get("mcp-session-id")
        if sent != state["valid"]:
            # A restarted server does not know the old session and never will.
            return httpx.Response(400, text="unknown session")
        return _fake_server()(request)

    async with respx.mock(assert_all_called=False) as router:
        router.post(URL).mock(side_effect=handler)
        manager = MCPManager({"echo": {"url": URL}}, enabled=True)

        assert await manager.call_tool("echo", "echo", {}) == {"content": [], "isError": False}

        state["valid"] = None  # server restarted; the held session id is dead forever
        with pytest.raises(MCPTransportError, match="HTTP 400"):
            await manager.call_tool("echo", "echo", {})

        # Recovery on the VERY NEXT call, via a fresh initialize.
        assert await manager.call_tool("echo", "echo", {}) == {"content": [], "isError": False}
        assert state["minted"] == 2
        await manager.close()


async def test_a_connect_failure_recovers_on_the_next_call():
    state = {"fail": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            raise httpx.ConnectError("refused")
        return _fake_server()(request)

    async with respx.mock(assert_all_called=False) as router:
        router.post(URL).mock(side_effect=handler)
        manager = MCPManager({"echo": {"url": URL}}, enabled=True)

        with pytest.raises(MCPTransportError):
            await manager.call_tool("echo", "echo", {})
        state["fail"] = False
        assert await manager.call_tool("echo", "echo", {}) == {"content": [], "isError": False}
        await manager.close()


# --------------------------------------------------------------------------
# Bounded bytes
# --------------------------------------------------------------------------


async def test_oversized_response_is_refused_before_a_single_byte_is_read():
    """A declared-huge body is refused off the header — the read never starts."""
    chunks_pulled = []

    def flood(payload):
        async def body():
            for i in range(5000):
                chunks_pulled.append(i)
                yield b"x" * 1024

        return httpx.Response(
            200,
            content=body(),
            headers={"content-type": "application/json", "content-length": "5120000"},
        )

    async with respx.mock(assert_all_called=False) as router:
        router.post(URL).mock(side_effect=_fake_server(on_call=flood))
        manager = MCPManager({"loud": {"url": URL, "max_response_bytes": 1024}}, enabled=True)
        with pytest.raises(MCPResponseTooLargeError, match="1024-byte cap"):
            await manager.call_tool("loud", "echo", {})
        assert chunks_pulled == [], "body was read despite a declared oversize length"
        await manager.close()


async def test_oversized_streamed_response_is_refused_without_content_length():
    """No content-length ⇒ the read is still aborted at the cap, not buffered whole."""
    chunks_pulled = []

    def flood(payload):
        async def body():
            for i in range(1000):
                chunks_pulled.append(i)
                yield b"x" * 1024

        return httpx.Response(200, content=body(), headers={"content-type": "application/json"})

    async with respx.mock(assert_all_called=False) as router:
        router.post(URL).mock(side_effect=_fake_server(on_call=flood))
        manager = MCPManager({"loud": {"url": URL, "max_response_bytes": 4096}}, enabled=True)
        with pytest.raises(MCPResponseTooLargeError):
            await manager.call_tool("loud", "echo", {})
        assert len(chunks_pulled) < 20, "read did not stop near the cap"
        await manager.close()


# --------------------------------------------------------------------------
# Malformed responses
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response_factory,expected",
    [
        (lambda p: httpx.Response(200, content=b"<html>nope</html>"), MCPProtocolError),
        (
            lambda p: httpx.Response(200, json={"jsonrpc": "1.0", "id": p["id"], "result": {}}),
            MCPProtocolError,
        ),
        (
            lambda p: httpx.Response(200, json={"jsonrpc": "2.0", "id": p["id"], "result": "text"}),
            MCPProtocolError,
        ),
        (
            lambda p: httpx.Response(200, json={"jsonrpc": "2.0", "id": 999, "result": {}}),
            MCPProtocolError,
        ),
        (lambda p: httpx.Response(200, json=[1, 2, 3]), MCPProtocolError),
        (
            lambda p: httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": p["id"],
                    "error": {"code": -32601, "message": "nope"},
                },
            ),
            MCPServerError,
        ),
        (lambda p: httpx.Response(500, text="boom"), MCPTransportError),
    ],
)
async def test_malformed_call_response_raises_typed_error(response_factory, expected):
    """Garbage from the server must raise, never return None to the caller."""
    async with respx.mock(assert_all_called=False) as router:
        router.post(URL).mock(side_effect=_fake_server(on_call=response_factory))
        manager = MCPManager({"echo": {"url": URL}}, enabled=True)
        with pytest.raises(expected):
            await manager.call_tool("echo", "echo", {})
        await manager.close()


@pytest.mark.parametrize(
    "tools_payload",
    [
        {"tools": "not-a-list"},
        {"tools": [{"description": "no name"}]},
        {"tools": [{"name": ""}]},
        {"tools": ["just-a-string"]},
        {},
    ],
)
async def test_malformed_tools_list_raises_typed_error(tools_payload):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload.get("method") == "initialize":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": payload["id"], "result": _INIT_RESULT}
            )
        if payload.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": payload["id"], "result": tools_payload}
        )

    async with respx.mock(assert_all_called=False) as router:
        router.post(URL).mock(side_effect=handler)
        manager = MCPManager({"echo": {"url": URL}}, enabled=True)
        with pytest.raises(MCPProtocolError):
            await manager.list_tools("echo")
        await manager.close()


async def test_initialize_without_protocol_version_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": payload["id"], "result": {"capabilities": {}}}
        )

    async with respx.mock(assert_all_called=False) as router:
        router.post(URL).mock(side_effect=handler)
        manager = MCPManager({"echo": {"url": URL}}, enabled=True)
        with pytest.raises(MCPProtocolError, match="protocolVersion"):
            await manager.list_tools("echo")
        await manager.close()


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------

SECRET = "zzb8-super-secret-token-value"


async def test_token_is_sent_but_never_logged_or_echoed_in_errors():
    """The credential must reach the wire and nothing else."""

    def explode(payload):
        # The server echoes garbage back — a plausible way for a token to leak.
        return httpx.Response(400, text=f"bad request for {SECRET}")

    async with respx.mock(assert_all_called=False) as router:
        route = router.post(URL).mock(side_effect=_fake_server(on_call=explode))
        manager = MCPManager({"echo": {"url": URL, "auth_token": SECRET}}, enabled=True)

        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(MCPTransportError) as excinfo,
        ):
            await manager.call_tool("echo", "echo", {"q": SECRET[:4]})

        # Not vacuous: the token really was used for auth.
        assert route.calls[0].request.headers["authorization"] == f"Bearer {SECRET}"
        assert SECRET not in str(excinfo.value)
        assert SECRET not in repr(manager.list_servers())
        assert SECRET not in repr(parse_server_config("echo", {"url": URL, "auth_token": SECRET}))
        rendered = json.dumps(logs, default=str)
        assert SECRET not in rendered, f"token leaked into a log event: {rendered}"
        await manager.close()


async def test_redirects_are_not_followed_so_auth_is_never_replayed_elsewhere():
    async with respx.mock(assert_all_called=False) as router:
        router.post(URL).mock(
            return_value=httpx.Response(307, headers={"Location": "https://zzb8-evil.invalid/x"})
        )
        evil = router.post("https://zzb8-evil.invalid/x").mock(
            return_value=httpx.Response(200, json={})
        )
        manager = MCPManager({"echo": {"url": URL, "auth_token": SECRET}}, enabled=True)

        with pytest.raises(MCPTransportError, match="HTTP 307"):
            await manager.call_tool("echo", "echo", {})
        assert evil.call_count == 0
        await manager.close()


# --------------------------------------------------------------------------
# Shutdown
# --------------------------------------------------------------------------


async def test_close_tears_down_the_http_client():
    async with respx.mock(assert_all_called=False) as router:
        router.post(URL).mock(side_effect=_fake_server())
        manager = MCPManager({"echo": {"url": URL}}, enabled=True)
        await manager.call_tool("echo", "echo", {})

        session = manager._sessions["echo"]
        client = session._client
        assert client is not None and client.is_closed is False

        await manager.close()

        assert client.is_closed is True
        assert manager._sessions == {}


async def test_close_on_an_unused_manager_is_a_noop():
    manager = MCPManager({"echo": {"url": URL}}, enabled=True)
    await manager.close()
