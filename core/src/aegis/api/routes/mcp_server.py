"""MCP **server** — AEGIS's own chat tools, served over streamable HTTP.

The mirror image of :mod:`aegis.mcp_manager` (the client). One endpoint,
``POST /api/mcp-server/{agent_id}``, speaks JSON-RPC 2.0 so an external agent
harness — ``claude`` / ``kimi`` CLI headless runs, Claude Desktop — can mount
AEGIS's GTD / knowledge / infra / money tools natively instead of shelling back
into the chat API. ``routes/mcp.py`` is the *client* admin surface and is
untouched by this module.

Serving tools to a third-party harness is a door into this AEGIS, so it is shut
by default and narrow when open:

* **Off by default.** ``settings.mcp_server_enabled`` (``AEGIS_MCP_SERVER_ENABLED``)
  gates every method, matching the client's default-deny posture. Off ⇒ 403.
* **Repo-standard auth.** The router carries the same ``verify_auth`` dependency
  as every other authenticated route — API key or Basic, no new scheme.
* **Per-agent tool gating.** The URL names an agent and the served *chat* tools
  are exactly that agent's ``metadata.tool_set`` (via ``_get_agent_tools``), so a
  mounted server can never reach past what that agent may already do in chat.
  ``call_mcp_tool`` is removed on top of that: serving it would let an MCP
  client re-enter AEGIS's MCP *client* (recursion, confused deputy). The one
  addition is ``approve_tool_use`` (below), which grants nothing.
* **Stateless.** No ``Mcp-Session-Id`` is ever issued and one sent by a client is
  ignored, so there is no server-side session to hijack, expire or leak. ``GET``
  is 405 (no server-initiated streams) and ``DELETE`` is a 204 no-op.

Responses are always ``application/json``; SSE is spec-legal but never used.
JSON-RPC *application* errors travel as a 200 with an ``error`` envelope on
purpose — a non-2xx status is a transport failure to a compliant client
(``mcp_manager._post`` raises before it ever decodes the body), which would hide
the message the caller needs.

One tool served here is **not** a chat tool: ``approve_tool_use``. It is the
target of a gated run's ``--permission-prompt-tool`` flag — the claude CLI calls
it instead of prompting a terminal nobody is sitting at, and blocks on the
answer. The handler raises an ``interactions`` card (the repo's universal HITL
primitive) and returns the operator's verdict. It is deliberately absent from
``CHAT_TOOLS``/``TOOL_EXECUTORS``: it is a transport-level permission gate, not
something an agent may call in chat.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError

from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.mcp_manager import _PROTOCOL_VERSION as MCP_PROTOCOL_VERSION
from aegis.services.agents import get_agent
from aegis.services.chat import (
    _MCP_TOOL_NAME,
    _TOOL_TIMEOUT_OVERRIDES,
    _execute_tool,
    _get_agent_tools,
    _schema_hint,
    _truncate_result,
    _truncate_text,
    _validate_tool_args,
)
from aegis.services.tools.base import ToolContext

logger = structlog.get_logger()

_SERVER_NAME = "aegis"

# JSON-RPC 2.0 reserved error codes.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602

# Tool results are consumed by an *engine* (a claude/kimi CLI run with a
# 200k-token context), not by the small local model driving the chat loop, so
# the 4 KB chat cap (settings.tool_result_max_bytes) would throw away output
# that is genuinely useful here. Still bounded — an unbounded result is a way
# to blow up the caller's context with one call.
_TOOL_RESULT_MAX_BYTES = 65_536

# Cap on the text of an executor failure echoed back to the caller.
_ERROR_CHARS = 300

# ── the permission gate (gated agent runs) ─────────────────────────────────
#
# `connectors/remote_script.py` launches a gated run with
# `--permission-prompt-tool mcp__aegis__approve_tool_use`. Both halves of that
# string are load-bearing: `aegis` is the server key in the run's MCP config,
# and this is the tool name. `tests/core/test_kimi_connector.py` asserts the two
# modules still agree.
APPROVAL_TOOL_NAME = "approve_tool_use"

# How long we hold the CLI's permission call open waiting for a human.
#
# It MUST stay below the `MCP_TOOL_TIMEOUT` the gated launch sets (600_000 ms =
# 10 min). If the CLI's timeout fired first, a slow operator would surface as a
# transport failure inside the run instead of a clean deny, and the card would
# be answered into the void. 9 min leaves a minute of headroom.
AGENT_RUN_APPROVAL_TIMEOUT_S = 540

# Bytes of the tool input shown on the card. A `Write` carries a whole file and
# a card is one chat message — but the operator still has to see enough to
# judge, so this is a plain head-cut, never a structural shrink that could drop
# the very key (`command`, `file_path`) the decision turns on.
_APPROVAL_PREVIEW_BYTES = 800

# Response values that mean "allow". Everything else — including a malformed or
# missing value — is a deny, because this is a permission gate.
_APPROVE_VALUES = frozenset({"approve", "approved", "allow"})

# Card buttons. `kind="choice"` rather than `kind="approval"` on purpose: the
# Slack renderer emits the option KEY as the response value for a choice, and so
# does the admin panel, so both surfaces answer `approve`/`deny`. The built-in
# `approval` kind disagrees across the two (`approve`/`reject` in Slack,
# `approved`/`rejected` in the admin panel) — harmless for a deny, but an
# approval that arrives under an unexpected name would be silently downgraded.
_APPROVAL_OPTIONS = {"approve": "✅ Approve", "deny": "⛔ Deny"}

_APPROVAL_TOOL_DESCRIPTOR = {
    "name": APPROVAL_TOOL_NAME,
    "description": (
        "Permission gate for a gated AEGIS agent run. The claude CLI calls this "
        "automatically via --permission-prompt-tool when it wants to use a tool that "
        "is not auto-allowed; it asks a human in the operator's chat channel and "
        "returns their verdict. Do NOT call it yourself — it grants nothing and only "
        "interrupts a person."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "tool_name": {
                "type": "string",
                "description": "The tool the run wants to use, e.g. 'Bash' or 'Write'.",
            },
            "input": {
                "type": "object",
                "description": "The arguments the run wants to call that tool with.",
            },
            "tool_use_id": {
                "type": "string",
                "description": "Optional correlation id for the pending tool use.",
            },
        },
        "required": ["tool_name", "input"],
    },
}


def _decision_allow(tool_input: dict) -> str:
    """The CLI expects the verdict JSON-stringified in the tool result text.

    `updatedInput` echoes the request VERBATIM: this gate decides whether a call
    happens, never what it does. Rewriting it here would let an approval the
    human read apply to a different call than the one they saw.
    """
    return json.dumps({"behavior": "allow", "updatedInput": tool_input})


def _decision_deny(message: str) -> str:
    return json.dumps({"behavior": "deny", "message": message})


def _approval_prompt(agent_id: str, tool_name: str, tool_input: dict) -> str:
    """The card text: whose run, which tool, and a bounded look at the input."""
    try:
        rendered = json.dumps(tool_input, indent=2, default=str, ensure_ascii=False)
    except (TypeError, ValueError):  # pragma: no cover — default=str covers ~everything
        rendered = str(tool_input)
    return (
        f"🔒 Gated run for *{agent_id}* wants to use `{tool_name}`.\n\n"
        f"```\n{_truncate_text(rendered, _APPROVAL_PREVIEW_BYTES)}\n```\n\n"
        "Approve and the run proceeds with exactly this input. Deny and the tool is "
        "blocked — the run keeps going and is told why."
    )


async def _handle_approve_tool_use(
    request: Request, agent_id: str, request_id: Any, args: dict
) -> JSONResponse:
    """Raise an approval card and block on it, then answer the CLI.

    **Fails closed, on every path.** No Temporal client, a malformed request, a
    workflow that will not start, a timeout, an unexpected exception — all of
    them deny. The only thing that produces an `allow` is a human resolving the
    card with an approve value. A permission gate that opens when it breaks is
    not a gate, so there is deliberately no `except` here that ends in an allow.

    The verdict is always a normal (`isError: false`) tool result: the CLI reads
    the decision out of the result text, and an error result would be a broken
    permission check rather than a deny.
    """
    tool_name = args.get("tool_name")
    tool_input = args.get("input")
    tool_use_id = str(args.get("tool_use_id") or "")[:64]

    if not isinstance(tool_name, str) or not tool_name.strip() or not isinstance(tool_input, dict):
        logger.warning(
            "mcp_approval_decision",
            agent_id=agent_id,
            tool=str(tool_name)[:64],
            decision="deny",
            reason="malformed_request",
        )
        return _tool_result(
            request_id,
            _decision_deny("Malformed permission request — denied."),
            is_error=False,
        )

    tool_name = tool_name.strip()
    # Length only. The input is the thing we are refusing to trust: it can carry
    # a file's contents, a token pasted into a command, someone's private text.
    input_bytes = len(json.dumps(tool_input, default=str).encode())
    # Read the module global at call time so a test can shrink the wait without
    # sleeping out the real 9-minute cap.
    timeout_s = AGENT_RUN_APPROVAL_TIMEOUT_S

    client = getattr(request.app.state, "temporal_client", None)
    if client is None:
        logger.warning(
            "mcp_approval_decision",
            agent_id=agent_id,
            tool=tool_name,
            decision="deny",
            reason="no_temporal_client",
            input_bytes=input_bytes,
        )
        return _tool_result(
            request_id,
            _decision_deny("Denied — AEGIS could not reach anyone to approve this."),
            is_error=False,
        )

    try:
        handle = await client.start_workflow(
            "InteractionFlow",
            {
                "agent_id": agent_id,
                "kind": "choice",
                "origin": "agent_run_gate",
                "prompt": _approval_prompt(agent_id, tool_name, tool_input),
                "options": _APPROVAL_OPTIONS,
                "timeout_seconds": int(timeout_s),
                # `archive` (not `hold`): the flow must give up on its own, or a
                # never-answered card leaves a workflow pending forever for a
                # run that stopped waiting minutes ago.
                "timeout_policy": "archive",
            },
            id=f"agent-run-approval-{uuid4().hex[:12]}",
            task_queue="aegis-main",
        )
        result = await asyncio.wait_for(handle.result(), timeout=timeout_s)
    except TimeoutError:
        logger.warning(
            "mcp_approval_decision",
            agent_id=agent_id,
            tool=tool_name,
            decision="deny",
            reason="timeout",
            input_bytes=input_bytes,
            tool_use_id=tool_use_id,
        )
        return _tool_result(
            request_id,
            _decision_deny(
                f"Denied — the operator did not respond within {int(timeout_s)}s."
            ),
            is_error=False,
        )
    except Exception as exc:  # noqa: BLE001 — a broken gate must deny, never allow
        logger.warning(
            "mcp_approval_decision",
            agent_id=agent_id,
            tool=tool_name,
            decision="deny",
            reason="error",
            error=type(exc).__name__,
            input_bytes=input_bytes,
            tool_use_id=tool_use_id,
        )
        return _tool_result(
            request_id,
            _decision_deny(
                f"Denied — the approval request failed ({type(exc).__name__}). "
                "Nothing was approved."
            ),
            is_error=False,
        )

    payload = result if isinstance(result, dict) else {}
    interaction_id = str(payload.get("interaction_id") or "")
    status = str(payload.get("status") or "")
    response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    value = str((response or {}).get("value") or "").strip().lower()
    note = str((response or {}).get("note") or "").strip()

    if status == "resolved" and value in _APPROVE_VALUES:
        logger.info(
            "mcp_approval_decision",
            agent_id=agent_id,
            tool=tool_name,
            decision="allow",
            interaction_id=interaction_id,
            input_bytes=input_bytes,
            tool_use_id=tool_use_id,
        )
        return _tool_result(request_id, _decision_allow(tool_input), is_error=False)

    if status == "resolved":
        message = "Denied by operator"
        if note:
            message = f"{message}: {note[:_ERROR_CHARS]}"
    else:
        # `archived` — the flow hit its own deadline first.
        message = "Denied — the operator did not respond in time."
    logger.info(
        "mcp_approval_decision",
        agent_id=agent_id,
        tool=tool_name,
        decision="deny",
        reason=status or "unresolved",
        interaction_id=interaction_id,
        input_bytes=input_bytes,
        tool_use_id=tool_use_id,
    )
    return _tool_result(request_id, _decision_deny(message), is_error=False)


def _require_enabled(settings: Settings = Depends(get_settings)) -> None:
    """Refuse every method while the server side is switched off."""
    if not getattr(settings, "mcp_server_enabled", False):
        raise HTTPException(
            status_code=403,
            detail=(
                "AEGIS MCP server is disabled — set AEGIS_MCP_SERVER_ENABLED=true "
                "and restart Core to serve AEGIS's chat tools to external MCP clients."
            ),
        )


router = APIRouter(
    prefix="/api/mcp-server",
    dependencies=[Depends(verify_auth), Depends(_require_enabled)],
)


def _rpc_result(request_id: Any, result: dict) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def _rpc_error(request_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    )


def _tool_result(request_id: Any, text: str, *, is_error: bool) -> JSONResponse:
    """An MCP ``tools/call`` result. A *tool* failure is not a protocol error —
    it comes back as ``isError: true`` so the calling model can read the reason
    and self-correct rather than seeing the transport blow up."""
    return _rpc_result(
        request_id, {"content": [{"type": "text", "text": text}], "isError": is_error}
    )


async def _load_agent(request: Request, agent_id: str) -> dict:
    """The agent row, or 404. Mirrors how the chat surfaces resolve an agent."""
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    agent = await get_agent(pool, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return agent


def _served_tools(agent_id: str, metadata: dict | None) -> list[dict]:
    """The agent's chat tools, minus the MCP passthrough.

    ``_get_agent_tools`` is the single source of truth for what an agent may
    call (DB ``metadata.tool_set`` → seed defaults → the minimal fallback set),
    so this surface can never be broader than the same agent's chat surface.
    ``call_mcp_tool`` is dropped: an MCP client calling it would drive AEGIS's
    own MCP client at a third-party server on its behalf.
    """
    return [
        tool
        for tool in _get_agent_tools(agent_id, metadata=metadata)
        if tool["function"]["name"] != _MCP_TOOL_NAME
    ]


def _tool_descriptor(tool: dict) -> dict:
    """One CHAT_TOOLS entry as an MCP tool descriptor."""
    fn = tool.get("function", {})
    schema = fn.get("parameters")
    return {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        # MCP names it inputSchema; the OpenAI-shaped registry names it
        # parameters. Same JSONSchema either way.
        "inputSchema": schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
    }


def _tool_context(request: Request, agent_id: str, settings: Settings) -> ToolContext:
    """Build the executor context exactly as the chat path does.

    ``task_id``/``chat_context`` are None: an MCP call has no Todoist anchor and
    no conversation around it. Every executor that reads ``chat_context`` does so
    as ``(ctx.chat_context or {})``, so None is the supported "no chat" value.
    ``mcp_manager`` is wired for parity with chat, but ``call_mcp_tool`` — its
    only consumer — is filtered out of the served set above.
    """
    state = request.app.state
    return ToolContext(
        agent_id=agent_id,
        task_id=None,
        knowledge_connector=getattr(state, "knowledge_connector", None),
        finance_connector=getattr(state, "finance_connector", None),
        chat_context=None,
        settings=settings,
        temporal_client=getattr(state, "temporal_client", None),
        search_connector=getattr(state, "search_connector", None),
        llm_client=getattr(state, "llm", None),
        remote_script_connector=getattr(state, "remote_script_connector", None),
        vercel_connector=getattr(state, "vercel_connector", None),
        mcp_manager=getattr(state, "mcp_manager", None),
        model_light=getattr(settings, "model_fast", "gemma4:e2b"),
    )


async def _handle_tools_call(
    request: Request,
    agent_id: str,
    settings: Settings,
    request_id: Any,
    params: Any,
    tools: list[dict],
) -> JSONResponse:
    """Validate, authorize and run one tool call."""
    if not isinstance(params, dict):
        return _rpc_error(request_id, _INVALID_PARAMS, "'params' must be an object")
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return _rpc_error(request_id, _INVALID_PARAMS, "'params.name' must be a tool name")
    args = params.get("arguments")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _rpc_error(request_id, _INVALID_PARAMS, "'params.arguments' must be an object")

    # The permission gate is served to every agent and has no CHAT_TOOLS entry,
    # so it is dispatched before the tool-set check rather than through it.
    if name == APPROVAL_TOOL_NAME:
        return await _handle_approve_tool_use(request, agent_id, request_id, args)

    if name not in {t["function"]["name"] for t in tools}:
        logger.info("mcp_server_tool_call", agent_id=agent_id, tool=name, status="not_granted")
        return _rpc_error(
            request_id,
            _INVALID_PARAMS,
            f"Unknown tool '{name}' — not in agent '{agent_id}' tool set",
        )

    # Log keys only: an argument value can carry personal text (a task title, a
    # note body), and this line lands in the shared log stream.
    arg_keys = sorted(args.keys())

    try:
        _validate_tool_args(name, args)
    except JSONSchemaValidationError as exc:
        hint = _schema_hint(name)
        message = f"{exc.message} — {hint}" if hint else exc.message
        logger.info(
            "mcp_server_tool_call",
            agent_id=agent_id,
            tool=name,
            status="invalid_args",
            arg_keys=arg_keys,
        )
        # Deliberately a tool result, not a JSON-RPC error: the schema hint is
        # what lets the calling model fix its own arguments and retry.
        return _tool_result(request_id, message, is_error=True)

    pool = request.app.state.db_pool
    ctx = _tool_context(request, agent_id, settings)
    timeout = _TOOL_TIMEOUT_OVERRIDES.get(
        name, getattr(settings, "tool_timeout_seconds", 30) or 30
    )
    try:
        result = await asyncio.wait_for(_execute_tool(pool, name, args, ctx), timeout=timeout)
    except TimeoutError:
        logger.warning(
            "mcp_server_tool_call",
            agent_id=agent_id,
            tool=name,
            status="timeout",
            arg_keys=arg_keys,
        )
        return _tool_result(
            request_id, f"Tool '{name}' timed out after {timeout}s", is_error=True
        )
    except Exception as exc:  # noqa: BLE001 — one bad tool must not 500 the endpoint
        logger.warning(
            "mcp_server_tool_call",
            agent_id=agent_id,
            tool=name,
            status="error",
            error=type(exc).__name__,
            arg_keys=arg_keys,
        )
        return _tool_result(
            request_id,
            f"Tool '{name}' failed: {type(exc).__name__}: {str(exc)[:_ERROR_CHARS]}",
            is_error=True,
        )

    text = _truncate_result(str(result), max_bytes=_TOOL_RESULT_MAX_BYTES)
    logger.info(
        "mcp_server_tool_call",
        agent_id=agent_id,
        tool=name,
        status="success",
        arg_keys=arg_keys,
        result_bytes=len(text.encode()),
    )
    return _tool_result(request_id, text, is_error=False)


@router.post("/{agent_id}")
async def mcp_server_endpoint(
    agent_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> Response:
    """Streamable-HTTP MCP endpoint serving `agent_id`'s chat tools.

    One JSON-RPC message per POST. Stateless: no `Mcp-Session-Id` is issued and
    one sent by the client is ignored.
    """
    agent = await _load_agent(request, agent_id)
    tools = _served_tools(agent_id, dict(agent.get("metadata") or {}))

    raw = await request.body()
    try:
        message = json.loads(raw)
    except ValueError:
        return _rpc_error(None, _PARSE_ERROR, "Parse error: body is not valid JSON")

    if isinstance(message, list):
        return _rpc_error(
            None,
            _INVALID_REQUEST,
            "Batch requests are not supported — send one JSON-RPC message per POST",
        )
    if not isinstance(message, dict):
        return _rpc_error(None, _INVALID_REQUEST, "A JSON-RPC message must be an object")

    method = message.get("method")
    # No `id` ⇒ a notification. JSON-RPC forbids a response to one, and the MCP
    # streamable-HTTP transport wants 202 + empty body.
    if "id" not in message:
        logger.debug("mcp_server_notification", agent_id=agent_id, method=str(method)[:64])
        return Response(status_code=202)

    request_id = message.get("id")
    if not isinstance(method, str) or not method:
        return _rpc_error(request_id, _INVALID_REQUEST, "'method' must be a string")

    if method == "initialize":
        # The client's requested protocolVersion is accepted whatever it is; we
        # answer with the revision we implement and let it negotiate down.
        logger.info("mcp_server_initialize", agent_id=agent_id, tools=len(tools))
        return _rpc_result(
            request_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": _SERVER_NAME, "version": request.app.version},
            },
        )

    if method == "ping":
        return _rpc_result(request_id, {})

    if method == "tools/list":
        # The permission gate rides along for every agent: `--permission-prompt-tool`
        # only resolves a tool the server actually advertises, and a gated run
        # whose gate is invisible cannot take a single non-allowed action.
        return _rpc_result(
            request_id,
            {"tools": [_tool_descriptor(t) for t in tools] + [_APPROVAL_TOOL_DESCRIPTOR]},
        )

    if method == "tools/call":
        return await _handle_tools_call(
            request, agent_id, settings, request_id, message.get("params"), tools
        )

    return _rpc_error(request_id, _METHOD_NOT_FOUND, f"Unknown method '{method}'")


@router.get("/{agent_id}")
async def mcp_server_no_stream(agent_id: str) -> Response:
    """No server-initiated SSE stream — the spec's way to say so is 405."""
    raise HTTPException(
        status_code=405,
        detail="This MCP endpoint is stateless and POST-only; it opens no server-initiated stream.",
        headers={"Allow": "POST, DELETE"},
    )


@router.delete("/{agent_id}", status_code=204)
async def mcp_server_end_session(agent_id: str) -> Response:
    """Session termination. No session is ever issued, so this is a no-op."""
    return Response(status_code=204)
