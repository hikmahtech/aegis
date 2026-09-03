"""POST /api/mcp-server/{agent_id} — AEGIS's chat tools served as an MCP server.

Real app, real Postgres pool, real agent row: the whole point of the endpoint is
that the tool list comes from the agent's DB `metadata.tool_set`, so a stubbed
pool would test nothing. Only the tool *executor* is swapped out (one dict entry
in `aegis.services.chat.TOOL_EXECUTORS`), because running the real one would
need Todoist.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
import structlog.testing
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.api.routes import mcp_server as mcp_server_mod
from aegis.config import Settings
from aegis.services import chat as chat_mod
from httpx import ASGITransport, AsyncClient

AGENT = "zzb9-mcp-agent"
UNKNOWN_AGENT = "zzb9-nobody"
PATH = f"/api/mcp-server/{AGENT}"
AUTH = {"X-API-Key": "test-key"}

# Every `_UNSERVED_TOOLS` member is granted in the DB on purpose: the endpoint
# must strip all of them. `call_mcp_tool` would re-enter AEGIS's MCP client; the
# next three each launch another CLI run, which mounts this same server again;
# `stop_agent_run` and `comment_on_task` are operator actions (the membership
# test below spells out why each one is withheld).
TOOL_SET = [
    "whats_next",
    "capture_to_inbox",
    "call_mcp_tool",
    "dispatch_agent_run",
    "aegis_self_diagnose",
    "investigate_resource",
    "stop_agent_run",
    "comment_on_task",
]


def _settings(
    *, enabled: bool = True, auth_disabled: bool = False, allow_unauthenticated: bool = False
) -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost/test",
        litellm_url="https://litellm.test/v1",
        temporal_ui_url="https://temporal.test",
        n8n_ui_url="https://n8n.test",
        admin_username="admin",
        admin_password="admin",
        n8n_webhook_secret="test-secret",
        api_key="test-key",
        mcp_server_enabled=enabled,
        auth_disabled=auth_disabled,
        mcp_server_allow_unauthenticated=allow_unauthenticated,
    )


def _app(settings: Settings, db_pool):
    app = create_app(run_lifespan=False)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.settings = settings
    app.state.db_pool = db_pool
    return app


@pytest_asyncio.fixture(loop_scope="function")
async def agent_row(db_pool):
    """A real agent whose tool_set is exactly TOOL_SET."""
    # The MCP surface records tool calls, a FK child of `agents`.
    await db_pool.execute("DELETE FROM chat_tool_calls WHERE agent_id = $1", AGENT)
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, metadata, active) "
        "VALUES ($1, 'Zzb9', 'test', '', $2, true)",
        AGENT,
        {"tool_set": TOOL_SET},
    )
    yield AGENT
    # The MCP surface records tool calls, a FK child of `agents`.
    await db_pool.execute("DELETE FROM chat_tool_calls WHERE agent_id = $1", AGENT)
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)


@pytest_asyncio.fixture(loop_scope="function")
async def client(db_pool, agent_row):
    app = _app(_settings(), db_pool)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _rpc(client, method: str, *, params=None, rpc_id=1, path: str = PATH, headers=AUTH):
    body: dict = {"jsonrpc": "2.0", "method": method}
    if rpc_id is not None:
        body["id"] = rpc_id
    if params is not None:
        body["params"] = params
    return await client.post(path, json=body, headers=headers)


# -- gates -----------------------------------------------------------------


async def test_disabled_setting_refuses_and_says_how_to_enable(db_pool, agent_row):
    app = _app(_settings(enabled=False), db_pool)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await _rpc(c, "tools/list")

    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert "AEGIS_MCP_SERVER_ENABLED" in detail
    assert "disabled" in detail


async def test_auth_disabled_refuses_to_serve_unauthenticated_tools(db_pool, agent_row):
    """`auth_disabled` + this endpoint = every agent's tools with no credential.

    `verify_auth` returns True unconditionally under AEGIS_AUTH_DISABLED (the
    documented posture for a proxy-fronted deployment), but the URL a run mounts
    is a LAN/overlay address that bypasses that proxy by design. The refusal has
    to name the way out, or an operator's only option is to guess.
    """
    app = _app(_settings(auth_disabled=True), db_pool)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await _rpc(c, "tools/list")

    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert "AEGIS_AUTH_DISABLED" in detail
    assert "AEGIS_MCP_SERVER_ALLOW_UNAUTHENTICATED" in detail


async def test_allow_unauthenticated_flag_serves_the_tools(db_pool, agent_row):
    """The escape hatch really is an escape hatch — and it is the ONLY thing
    that changed between this test and the one above."""
    app = _app(_settings(auth_disabled=True, allow_unauthenticated=True), db_pool)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        # No credentials at all: auth_disabled is what makes this reach the route.
        resp = await _rpc(c, "tools/list", headers={})

    assert resp.status_code == 200, resp.text
    assert "whats_next" in [t["name"] for t in resp.json()["result"]["tools"]]


async def test_disabled_server_still_403s_even_with_the_allow_flag(db_pool, agent_row):
    """The unauthenticated escape hatch does not switch the server ON."""
    app = _app(_settings(enabled=False, auth_disabled=True, allow_unauthenticated=True), db_pool)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await _rpc(c, "tools/list", headers={})
    assert resp.status_code == 403, resp.text
    assert "AEGIS_MCP_SERVER_ENABLED" in resp.json()["detail"]


async def test_auth_is_required(client):
    """Missing and wrong credentials both get the repo-standard 401."""
    missing = await _rpc(client, "tools/list", headers={})
    wrong = await _rpc(client, "tools/list", headers={"X-API-Key": "zzb9-not-the-key"})
    assert missing.status_code == 401, missing.text
    assert wrong.status_code == 401, wrong.text
    # The refusal is total: no JSON-RPC envelope leaks out of an unauthed call.
    assert "jsonrpc" not in missing.text


async def test_unknown_agent_is_404(client):
    resp = await _rpc(client, "tools/list", path=f"/api/mcp-server/{UNKNOWN_AGENT}")
    assert resp.status_code == 404, resp.text
    assert UNKNOWN_AGENT in resp.json()["detail"]


# -- protocol --------------------------------------------------------------


async def test_initialize_advertises_tools_capability(client):
    resp = await _rpc(client, "initialize", params={"protocolVersion": "2024-11-05"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    result = body["result"]
    assert isinstance(result["protocolVersion"], str) and result["protocolVersion"]
    assert result["capabilities"]["tools"] == {}
    assert result["serverInfo"]["name"] == "aegis"
    assert result["serverInfo"]["version"]
    # Stateless: no session handle is minted for the client to carry.
    assert "mcp-session-id" not in {k.lower() for k in resp.headers}


async def test_notification_is_accepted_with_no_body(client):
    resp = await client.post(
        PATH,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=AUTH,
    )
    assert resp.status_code == 202, resp.text
    assert resp.content == b""


async def test_ping_returns_empty_result(client):
    resp = await _rpc(client, "ping", rpc_id="p1")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"jsonrpc": "2.0", "id": "p1", "result": {}}


async def test_unknown_method_is_method_not_found(client):
    resp = await _rpc(client, "resources/list", rpc_id=7)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == 7
    assert body["error"]["code"] == -32601
    assert "resources/list" in body["error"]["message"]
    assert "result" not in body


async def test_malformed_json_is_a_parse_error(client):
    resp = await client.post(PATH, content=b"{not json", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["error"]["code"] == -32700
    assert body["id"] is None


async def test_get_is_405_and_delete_is_204(client):
    got = await client.get(PATH, headers=AUTH)
    deleted = await client.delete(PATH, headers=AUTH)
    assert got.status_code == 405, got.text
    assert got.headers.get("allow") == "POST, DELETE"
    assert deleted.status_code == 204, deleted.text


# -- tools/list ------------------------------------------------------------


async def test_tools_list_is_the_agents_set_minus_the_unserved_tools(client):
    resp = await _rpc(client, "tools/list")
    assert resp.status_code == 200, resp.text
    # `approve_tool_use` is the permission gate, not a chat tool the agent was
    # granted — it has its own test below and is excluded here so this one keeps
    # asserting exactly what `metadata.tool_set` produces.
    tools = [t for t in resp.json()["result"]["tools"] if t["name"] != "approve_tool_use"]

    assert [t["name"] for t in sorted(tools, key=lambda t: t["name"])] == [
        "capture_to_inbox",
        "whats_next",
    ]
    # Proves the filter did the work: the DB really granted every excluded tool.
    assert set(mcp_server_mod._UNSERVED_TOOLS) <= set(TOOL_SET)

    by_name = {t["name"] for t in tools}
    assert by_name.isdisjoint(mcp_server_mod._UNSERVED_TOOLS)

    schemas = {
        spec["function"]["name"]: spec["function"]["parameters"]
        for spec in chat_mod.CHAT_TOOLS
        if spec["function"]["name"] in by_name
    }
    for tool in tools:
        assert isinstance(tool["inputSchema"], dict)
        assert tool["inputSchema"] == schemas[tool["name"]]
        assert tool["description"]


# -- tools/call ------------------------------------------------------------


async def test_tools_call_round_trips_the_executor_result(client, monkeypatch):
    captured: dict = {}

    async def _fake(pool, args, ctx):
        captured["args"] = args
        captured["agent_id"] = ctx.agent_id
        captured["chat_context"] = ctx.chat_context
        return json.dumps({"tasks": ["ship the MCP server"]})

    monkeypatch.setitem(chat_mod.TOOL_EXECUTORS, "whats_next", _fake)

    with structlog.testing.capture_logs() as logs:
        resp = await _rpc(
            client,
            "tools/call",
            params={"name": "whats_next", "arguments": {"limit": 424242, "energy": "high"}},
        )

    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == {"tasks": ["ship the MCP server"]}
    assert result["content"][0]["type"] == "text"
    # The executor really ran, with the caller's arguments and this agent's id.
    assert captured["args"] == {"limit": 424242, "energy": "high"}
    assert captured["agent_id"] == AGENT
    assert captured["chat_context"] is None

    call_logs = [entry for entry in logs if entry.get("event") == "mcp_server_tool_call"]
    assert [entry["status"] for entry in call_logs] == ["success"]
    assert call_logs[0]["arg_keys"] == ["energy", "limit"]
    # Argument VALUES must never reach the log stream — only their keys.
    flat = json.dumps(call_logs[0])
    assert "424242" not in flat
    assert "high" not in flat


async def test_tools_call_with_invalid_args_returns_the_schema_hint(client):
    resp = await _rpc(
        client,
        "tools/call",
        # `text` is required by capture_to_inbox's schema.
        params={"name": "capture_to_inbox", "arguments": {"source": "chat"}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" not in body  # a bad argument is a tool result, not a protocol error
    result = body["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "text" in text and "required" in text
    assert "Expected arguments" in text  # _schema_hint's prefix — the self-correct cue


async def test_tools_call_for_an_ungranted_tool_is_invalid_params(client):
    # A real tool with a real executor, deliberately not in this agent's set.
    assert "system_status" in chat_mod.TOOL_EXECUTORS
    assert "system_status" not in TOOL_SET

    resp = await _rpc(
        client, "tools/call", params={"name": "system_status", "arguments": {}}, rpc_id=9
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == 9
    assert body["error"]["code"] == -32602
    assert "system_status" in body["error"]["message"]
    assert "result" not in body


@pytest.mark.parametrize("name", sorted(mcp_server_mod._UNSERVED_TOOLS))
async def test_unserved_tools_cannot_be_invoked_even_though_they_are_granted(client, name):
    """Filtered from tools/list AND refused on call.

    Both halves matter and they are one derivation (`_served_tools`), so this
    parametrized pair is what proves the derivation is shared: a filter applied
    only at listing time would leave every one of these callable by name.

    `dispatch_agent_run` / `aegis_self_diagnose` / `investigate_resource` each
    START ANOTHER CLI RUN, and a run mounts this endpoint with the same tool
    set — serving them is unbounded recursion with no depth counter anywhere.
    `call_mcp_tool` re-enters AEGIS's MCP *client* (confused deputy).
    """
    assert name in TOOL_SET, "the DB grant is what makes this test non-vacuous"

    listed = await _rpc(client, "tools/list")
    assert name not in [t["name"] for t in listed.json()["result"]["tools"]]

    called = await _rpc(client, "tools/call", params={"name": name, "arguments": {}})
    assert called.status_code == 200, called.text
    body = called.json()
    assert body["error"]["code"] == -32602
    assert "result" not in body  # nothing ran


async def test_the_unserved_set_is_exactly_these_six(client):
    """A closed list, asserted by name: adding a run-spawning tool to an agent's
    `tool_set` without adding it here silently re-opens the recursion door, and
    the only way to notice is a test that pins the membership.

    `stop_agent_run` joined for a different reason than the rest — it does not
    spawn anything, it KILLS. A run able to call it could take out a sibling run
    or the very run a human is waiting on, so it is operator-only too.

    `comment_on_task` is a third reason again: what it posts is a user-authored
    note, and a user-authored note is exactly what starts a task session's next
    turn. A run holding it could comment its own task into a self-triggering
    loop, so it lives on the operator mount and in chat only."""
    assert set(mcp_server_mod._UNSERVED_TOOLS) == {
        "call_mcp_tool",
        "dispatch_agent_run",
        "aegis_self_diagnose",
        "investigate_resource",
        "stop_agent_run",
        "comment_on_task",
    }
    # Every one of them is a real chat tool with a real executor — a typo here
    # would exclude nothing at all.
    for name in mcp_server_mod._UNSERVED_TOOLS:
        assert name in chat_mod.TOOL_EXECUTORS


async def test_raising_executor_becomes_an_is_error_result_not_a_500(client, monkeypatch):
    async def _boom(pool, args, ctx):
        raise RuntimeError("zzb9 executor exploded")

    monkeypatch.setitem(chat_mod.TOOL_EXECUTORS, "whats_next", _boom)

    resp = await _rpc(client, "tools/call", params={"name": "whats_next", "arguments": {}})

    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "whats_next" in text
    assert "RuntimeError" in text
    assert "zzb9 executor exploded" in text


async def test_executor_is_restored_after_monkeypatching(client):
    """Guards the two tests above: a leaked patch would make them vacuous."""
    assert chat_mod.TOOL_EXECUTORS["whats_next"] is chat_mod._exec_whats_next


@pytest.mark.parametrize("body", [[{"jsonrpc": "2.0", "id": 1, "method": "ping"}], "nope", 5])
async def test_non_object_message_is_an_invalid_request(client, body):
    resp = await client.post(PATH, json=body, headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["error"]["code"] == -32600


# -- approve_tool_use (the gated-run permission gate) -----------------------
#
# The claude CLI calls this via `--permission-prompt-tool` and reads the verdict
# out of the result TEXT as a JSON-stringified object. Every test below decodes
# that text, because a handler that returned the right HTTP shape with the wrong
# decision inside would be a gate that silently opens.

APPROVE_ARGS = {
    "tool_name": "Bash",
    "input": {"command": "rm -rf /srv/data", "description": "clean the volume"},
    "tool_use_id": "toolu_zzb9",
}


def _fake_temporal(result=None, *, result_exc=None, start_exc=None, result_delay=0.0):
    """A Temporal client stub standing in for the InteractionFlow round trip."""
    handle = MagicMock()

    async def _result():
        if result_delay:
            await asyncio.sleep(result_delay)
        if result_exc is not None:
            raise result_exc
        return result

    handle.result = _result
    client = AsyncMock()
    if start_exc is not None:
        client.start_workflow.side_effect = start_exc
    else:
        client.start_workflow.return_value = handle
    return client


@pytest_asyncio.fixture(loop_scope="function")
async def gated_client(db_pool, agent_row):
    """Client factory bound to a stubbed Temporal client."""

    async def _make(temporal):
        app = _app(_settings(), db_pool)
        app.state.temporal_client = temporal
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _decide(gated_client, temporal, args=None):
    """Call approve_tool_use and return (decision_dict, raw_result)."""
    async with await gated_client(temporal) as c:
        resp = await _rpc(
            c,
            "tools/call",
            params={"name": "approve_tool_use", "arguments": APPROVE_ARGS if args is None else args},
        )
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    return json.loads(result["content"][0]["text"]), result


async def test_approve_tool_use_is_served_alongside_the_agents_tools(client):
    """Served to every agent regardless of tool_set: `--permission-prompt-tool`
    can only resolve a tool the server advertises."""
    resp = await _rpc(client, "tools/list")
    tools = {t["name"]: t for t in resp.json()["result"]["tools"]}

    # The agent's own gated tools are still there — this is an addition, not a
    # replacement.
    assert "whats_next" in tools
    assert "capture_to_inbox" in tools

    gate = tools["approve_tool_use"]
    assert gate["inputSchema"]["required"] == ["tool_name", "input"]
    assert gate["inputSchema"]["properties"]["input"]["type"] == "object"
    assert "tool_use_id" in gate["inputSchema"]["properties"]
    assert gate["description"]

    # It is NOT a chat tool: no CHAT_TOOLS schema and no executor, so no agent
    # can reach it through the chat loop and no tool_set can grant it.
    assert all(t["function"]["name"] != "approve_tool_use" for t in chat_mod.CHAT_TOOLS)
    assert "approve_tool_use" not in chat_mod.TOOL_EXECUTORS


async def test_approve_returns_allow_with_the_input_verbatim(gated_client):
    temporal = _fake_temporal(
        {"interaction_id": "int-1", "status": "resolved", "response": {"value": "approve"}}
    )
    with structlog.testing.capture_logs() as logs:
        decision, result = await _decide(gated_client, temporal)

    assert decision["behavior"] == "allow"
    # Verbatim: the human approved THIS call, so the gate must not rewrite it.
    assert decision["updatedInput"] == APPROVE_ARGS["input"]
    assert result["isError"] is False

    # The card really was raised, as an InteractionFlow on the worker's queue.
    temporal.start_workflow.assert_awaited_once()
    args, kwargs = temporal.start_workflow.call_args
    assert args[0] == "InteractionFlow"
    spawn = args[1]
    assert spawn["agent_id"] == AGENT
    assert spawn["origin"] == "agent_run_gate"
    assert set(spawn["options"]) == {"approve", "deny"}
    assert spawn["timeout_policy"] == "archive"
    assert spawn["timeout_seconds"] == mcp_server_mod.AGENT_RUN_APPROVAL_TIMEOUT_S
    assert kwargs["task_queue"] == "aegis-main"
    # The card names the agent, the tool, and shows the input.
    assert AGENT in spawn["prompt"]
    assert "Bash" in spawn["prompt"]
    assert "rm -rf /srv/data" in spawn["prompt"]

    decided = [entry for entry in logs if entry.get("event") == "mcp_approval_decision"]
    assert [entry["decision"] for entry in decided] == ["allow"]
    assert decided[0]["tool"] == "Bash"
    assert decided[0]["input_bytes"] > 0
    # The input may carry a secret or someone's private text — length only.
    assert "rm -rf /srv/data" not in json.dumps(decided[0])


async def test_deny_carries_the_operators_reason(gated_client):
    temporal = _fake_temporal(
        {
            "interaction_id": "int-2",
            "status": "resolved",
            "response": {"value": "deny", "note": "that volume is production"},
        }
    )
    decision, _ = await _decide(gated_client, temporal)

    assert decision["behavior"] == "deny"
    assert "that volume is production" in decision["message"]
    assert "updatedInput" not in decision


async def test_timeout_denies_rather_than_hanging_or_allowing(gated_client, monkeypatch):
    """The wait is capped below the CLI's MCP tool timeout, so a slow operator
    becomes a deny AEGIS chose, not a transport failure inside the run."""
    monkeypatch.setattr(mcp_server_mod, "AGENT_RUN_APPROVAL_TIMEOUT_S", 0.05)
    # Answer far later than the cap; the cap is what must end the call.
    temporal = _fake_temporal({"status": "resolved", "response": {"value": "approve"}},
                              result_delay=30)

    decision, result = await _decide(gated_client, temporal)

    assert decision["behavior"] == "deny"
    assert "did not respond" in decision["message"]
    assert result["isError"] is False
    # The cap really was the injected one, not the 9-minute production value —
    # so this test could not have passed by waiting the real cap out.
    assert temporal.start_workflow.call_args[0][1]["timeout_seconds"] < 1
    assert mcp_server_mod.AGENT_RUN_APPROVAL_TIMEOUT_S != 540


async def test_archived_card_is_a_deny(gated_client):
    """`timeout_policy='archive'` — the flow gave up before we did."""
    temporal = _fake_temporal({"interaction_id": "int-3", "status": "archived", "response": None})
    decision, _ = await _decide(gated_client, temporal)
    assert decision["behavior"] == "deny"


@pytest.mark.parametrize(
    "temporal_kwargs",
    [
        {"start_exc": RuntimeError("temporal frontend unreachable")},
        {"result_exc": RuntimeError("workflow task failed")},
    ],
    ids=["start_workflow_raises", "result_raises"],
)
async def test_a_broken_approval_path_denies_it_never_allows(gated_client, temporal_kwargs):
    """FAIL CLOSED. This is a permission gate: every internal failure must deny.

    Falsifiable by construction — mapping an exception to
    `{"behavior": "allow", ...}` (or to an MCP `isError` the CLI might treat as
    "no opinion") flips these assertions.
    """
    temporal = _fake_temporal(**temporal_kwargs)
    with structlog.testing.capture_logs() as logs:
        decision, result = await _decide(gated_client, temporal)

    assert decision["behavior"] == "deny"
    assert decision["behavior"] != "allow"
    assert "updatedInput" not in decision
    assert result["isError"] is False  # a decision, not a broken permission check
    decided = [entry for entry in logs if entry.get("event") == "mcp_approval_decision"]
    assert [entry["decision"] for entry in decided] == ["deny"]


async def test_no_temporal_client_denies(db_pool, agent_row):
    """No approval channel ⇒ nobody can approve ⇒ nothing is approved."""
    app = _app(_settings(), db_pool)
    assert getattr(app.state, "temporal_client", None) is None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await _rpc(
            c, "tools/call", params={"name": "approve_tool_use", "arguments": APPROVE_ARGS}
        )
    decision = json.loads(resp.json()["result"]["content"][0]["text"])
    assert decision["behavior"] == "deny"


@pytest.mark.parametrize(
    "args",
    [
        {"input": {"command": "ls"}},  # no tool_name
        {"tool_name": "Bash"},  # no input
        {"tool_name": "Bash", "input": "rm -rf /"},  # input not an object
        {"tool_name": "", "input": {}},  # empty tool_name
    ],
)
async def test_malformed_permission_request_denies_without_raising_a_card(gated_client, args):
    temporal = _fake_temporal(
        {"status": "resolved", "response": {"value": "approve"}}
    )
    decision, _ = await _decide(gated_client, temporal, args=args)
    assert decision["behavior"] == "deny"
    # No human was bothered for a request we could not even describe.
    temporal.start_workflow.assert_not_called()


@pytest.mark.parametrize("value", ["deny", "reject", "", "APPROVE_LATER", None])
async def test_only_an_explicit_approve_value_allows(gated_client, value):
    """Anything the gate does not positively recognise as approval is a deny."""
    temporal = _fake_temporal(
        {"interaction_id": "int-4", "status": "resolved", "response": {"value": value}}
    )
    decision, _ = await _decide(gated_client, temporal)
    assert decision["behavior"] == "deny"


async def test_approval_gate_needs_no_tool_set_grant(db_pool):
    """No `tool_set` grant can produce the gate, so an agent that never asked
    for it still gets it — otherwise a locked-down agent could never run gated.

    The agent below stores `tool_set: []`, which `_get_agent_tools` reads as
    *unconfigured* (falling back to `_FALLBACK_TOOL_SET`), not as "no tools" —
    so this is "the gate rides along regardless of the set", not "the gate
    survives an empty set".
    """
    bare = "zzb9-bare-agent"
    # The MCP surface records tool calls, a FK child of `agents`.
    await db_pool.execute("DELETE FROM chat_tool_calls WHERE agent_id = $1", bare)
    await db_pool.execute("DELETE FROM agents WHERE id = $1", bare)
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, metadata, active) "
        "VALUES ($1, 'Bare', 'test', '', $2, true)",
        bare,
        {"tool_set": []},
    )
    try:
        app = _app(_settings(), db_pool)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await _rpc(c, "tools/list", path=f"/api/mcp-server/{bare}")
        names = [t["name"] for t in resp.json()["result"]["tools"]]
        assert "approve_tool_use" in names
    finally:
        # The MCP surface records tool calls, a FK child of `agents`.
        await db_pool.execute("DELETE FROM chat_tool_calls WHERE agent_id = $1", bare)
        await db_pool.execute("DELETE FROM agents WHERE id = $1", bare)
