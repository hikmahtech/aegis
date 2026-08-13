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
* **Per-agent tool gating.** The URL names an agent and the served tool list is
  exactly that agent's ``metadata.tool_set`` (via ``_get_agent_tools``), so a
  mounted server can never reach past what that agent may already do in chat.
  ``call_mcp_tool`` is removed on top of that: serving it would let an MCP
  client re-enter AEGIS's MCP *client* (recursion, confused deputy).
* **Stateless.** No ``Mcp-Session-Id`` is ever issued and one sent by a client is
  ignored, so there is no server-side session to hijack, expire or leak. ``GET``
  is 405 (no server-initiated streams) and ``DELETE`` is a 204 no-op.

Responses are always ``application/json``; SSE is spec-legal but never used.
JSON-RPC *application* errors travel as a 200 with an ``error`` envelope on
purpose — a non-2xx status is a transport failure to a compliant client
(``mcp_manager._post`` raises before it ever decodes the body), which would hide
the message the caller needs.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

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
        return _rpc_result(request_id, {"tools": [_tool_descriptor(t) for t in tools]})

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
