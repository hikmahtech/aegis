"""MCP server introspection + tool dispatch endpoints.

Every MCP failure mode is a typed :class:`~aegis.mcp_manager.MCPError`, mapped
here to a status code. Nothing leaks a credential: the manager's messages name
the *server config key* only, never the URL or the token.
"""

from fastapi import APIRouter, Depends, HTTPException, Request

from aegis.api.auth import verify_auth
from aegis.mcp_manager import (
    MCPDisabledError,
    MCPError,
    MCPTimeoutError,
    MCPUnknownServerError,
)

router = APIRouter(prefix="/api/mcp", dependencies=[Depends(verify_auth)])


def _manager(request: Request):
    """The MCP manager, refusing early when the subsystem is off (503)."""
    mcp_manager = getattr(request.app.state, "mcp_manager", None)
    if not mcp_manager:
        raise HTTPException(503, "MCP manager not initialized")
    try:
        mcp_manager.ensure_enabled()
    except MCPDisabledError as exc:
        raise HTTPException(503, str(exc)) from exc
    return mcp_manager


def _known_server(request: Request, server: str) -> None:
    settings = request.app.state.settings
    if server not in (settings.mcp_servers or {}):
        raise HTTPException(404, f"MCP server '{server}' not configured")


def _as_http(exc: MCPError) -> HTTPException:
    if isinstance(exc, MCPDisabledError):
        return HTTPException(503, str(exc))
    if isinstance(exc, MCPUnknownServerError):
        return HTTPException(404, str(exc))
    if isinstance(exc, MCPTimeoutError):
        return HTTPException(504, str(exc))
    return HTTPException(500, f"MCP call failed: {exc}")


@router.get("")
async def list_mcp_servers(request: Request):
    """Configured MCP servers and their config health (never their tokens)."""
    mcp_manager = _manager(request)
    return {"ok": True, "servers": mcp_manager.list_servers()}


@router.get("/{server}/tools")
async def list_mcp_tools(server: str, request: Request):
    """Discover a server's tools. Discovery only — nothing is executed."""
    mcp_manager = _manager(request)
    _known_server(request, server)
    try:
        tools = await mcp_manager.list_tools(server)
    except MCPError as exc:
        raise _as_http(exc) from exc
    except Exception as exc:
        raise HTTPException(500, f"MCP call failed: {exc}") from exc
    return {"ok": True, "server": server, "tools": tools}


@router.post("/{server}/{tool}")
async def call_mcp_tool(server: str, tool: str, request: Request):
    """Call a tool on a named MCP server."""
    mcp_manager = _manager(request)
    _known_server(request, server)

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(422, "MCP tool arguments must be a JSON object")
    try:
        result = await mcp_manager.call_tool(server, tool, body)
        return {"ok": True, "result": result}
    except MCPError as exc:
        raise _as_http(exc) from exc
    except Exception as exc:
        raise HTTPException(500, f"MCP call failed: {exc}") from exc
