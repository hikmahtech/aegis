"""MCP client connection manager — stub.

No MCP client is wired up yet: ``call_tool`` raises ``NotImplementedError``
and the ``/api/mcp`` route translates that to HTTP 501. The stub keeps the
route's 404/501/auth contract intact (see tests/core/test_mcp_endpoint.py)
until a real transport lands.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()


class MCPManager:
    """Validate MCP server config and reject tool calls until a client lands."""

    def __init__(self, server_configs: dict[str, dict]):
        self._configs = server_configs

    async def call_tool(self, server: str, tool: str, args: dict) -> Any:
        """Reject the call — no MCP client is implemented yet."""
        config = self._configs.get(server)
        if not config:
            raise ValueError(f"MCP server '{server}' not configured")

        logger.info("mcp_call", server=server, tool=tool, args_keys=list(args.keys()))
        raise NotImplementedError(
            f"MCP client for '{server}' not yet implemented. "
            f"Server config: {config.get('transport', 'unknown')} transport."
        )

    async def close(self) -> None:
        """No-op — no connections are opened yet."""
