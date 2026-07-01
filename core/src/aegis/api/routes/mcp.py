"""MCP tool dispatch endpoint."""

from fastapi import APIRouter, Depends, HTTPException, Request

from aegis.api.auth import verify_auth

router = APIRouter(prefix="/api/mcp", dependencies=[Depends(verify_auth)])


@router.post("/{server}/{tool}")
async def call_mcp_tool(server: str, tool: str, request: Request):
    """Call a tool on a named MCP server."""
    settings = request.app.state.settings
    server_config = (settings.mcp_servers or {}).get(server)
    if not server_config:
        raise HTTPException(404, f"MCP server '{server}' not configured")

    mcp_manager = getattr(request.app.state, "mcp_manager", None)
    if not mcp_manager:
        raise HTTPException(503, "MCP manager not initialized")

    body = await request.json()
    try:
        result = await mcp_manager.call_tool(server, tool, body)
        return {"ok": True, "result": result}
    except NotImplementedError as exc:
        raise HTTPException(501, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"MCP call failed: {exc}") from exc
