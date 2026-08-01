"""MCP client — streamable-HTTP transport against external MCP servers.

An MCP server is a **remote party** whose tool definitions and responses we do
not control, so every response here is treated as untrusted input:

* **Fail closed.** The whole subsystem is gated on ``settings.mcp_enabled``
  (default off). Off ⇒ zero servers, no socket is ever opened, every call
  raises :class:`MCPDisabledError`. A server entry that declares ``auth_token``
  but leaves it blank is *rejected*, never downgraded to an anonymous connect.
* **Bounded.** Connect timeout, read timeout, a total per-call timeout, and a
  hard cap on response bytes. A server that hangs or floods degrades to a typed
  error; it can neither block a request handler forever nor OOM the process.
* **Validated.** Malformed JSON-RPC raises :class:`MCPProtocolError` rather than
  propagating ``None`` into a caller.
* **No credential ever reaches a log or an error string.** Errors name the
  *server* (a config key), never the URL or the token.
* **Nothing is executed on connect.** ``initialize`` + ``tools/list`` are pure
  discovery; a tool only runs when :meth:`MCPManager.call_tool` is called
  explicitly. Deciding *which* agent may call *which* tool is not this module's
  job (see B9) — this module deliberately grants nobody anything.

The **stdio transport is deliberately not implemented.** stdio MCP means
spawning a local process per configured server, i.e. arbitrary local code
execution driven by config; that surface needs its own review and is not
smuggled in here. A ``transport: stdio`` entry is rejected with an explicit
message instead of being silently ignored.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
import structlog

logger = structlog.get_logger()

# MCP revision we advertise. Servers negotiate down; we do not require a match.
_PROTOCOL_VERSION = "2025-06-18"
_CLIENT_NAME = "aegis"
_CLIENT_VERSION = "0.1.0"

_DEFAULT_TIMEOUT_S = 30.0
_MAX_TIMEOUT_S = 120.0
_CONNECT_TIMEOUT_S = 10.0
_DEFAULT_MAX_RESPONSE_BYTES = 1_000_000
_MAX_RESPONSE_BYTES_CEILING = 8_000_000
_TOOLS_CACHE_TTL_S = 300.0
_ERROR_CHARS = 300

_SUPPORTED_TRANSPORTS = ("streamable-http", "http")


class MCPError(Exception):
    """Base class for every MCP client failure."""


class MCPDisabledError(MCPError):
    """The MCP subsystem is switched off — nothing was contacted."""


class MCPConfigError(MCPError):
    """A configured server entry is unusable (bad shape, unsupported transport)."""


class MCPUnknownServerError(MCPError):
    """No server is configured under that name."""


class MCPTimeoutError(MCPError):
    """The server did not answer within the per-call budget."""


class MCPResponseTooLargeError(MCPError):
    """The server's response exceeded the byte cap and was abandoned."""


class MCPProtocolError(MCPError):
    """The server's response was not a well-formed JSON-RPC result."""


class MCPServerError(MCPError):
    """The server answered with a JSON-RPC error object."""


class MCPTransportError(MCPError):
    """The connection failed, or the server returned a non-2xx status."""


@dataclass(frozen=True)
class MCPServerConfig:
    """One validated entry of ``settings.mcp_servers``.

    ``auth_token`` is ``repr=False`` so the credential cannot reach a log line,
    a traceback frame summary or a structlog event by accident.
    """

    name: str
    url: str
    transport: str = "streamable-http"
    auth_token: str = field(default="", repr=False)
    timeout_s: float = _DEFAULT_TIMEOUT_S
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES


def _positive_number(raw: Any, name: str, label: str, ceiling: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise MCPConfigError(f"server '{name}': {label} must be a number") from None
    if value <= 0 or value > ceiling:
        raise MCPConfigError(f"server '{name}': {label} must be in (0, {ceiling:g}]")
    return value


def parse_server_config(name: str, raw: Any) -> MCPServerConfig:
    """Validate one ``mcp_servers`` entry. Raises :class:`MCPConfigError`.

    The message never contains the token or the URL's credentials — only the
    config key, so it is safe to log and to return to an admin caller.
    """
    if not isinstance(raw, dict):
        raise MCPConfigError(f"server '{name}': config must be an object")

    transport = str(raw.get("transport") or "streamable-http").strip().lower()
    if transport == "stdio":
        raise MCPConfigError(
            f"server '{name}': stdio transport is not supported — it spawns a local "
            "process. Use transport 'streamable-http' with a url."
        )
    if transport not in _SUPPORTED_TRANSPORTS:
        raise MCPConfigError(f"server '{name}': unknown transport '{transport}'")

    url = str(raw.get("url") or "").strip()
    if not url:
        raise MCPConfigError(f"server '{name}': 'url' is required for the http transport")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise MCPConfigError(f"server '{name}': 'url' must be an absolute http(s) URL")

    # Fail closed: declaring auth and leaving it blank must NOT silently become
    # an anonymous connection to a server that was meant to be authenticated.
    auth_token = ""
    if "auth_token" in raw:
        auth_token = str(raw.get("auth_token") or "").strip()
        if not auth_token:
            raise MCPConfigError(
                f"server '{name}': 'auth_token' is declared but empty — refusing to "
                "connect anonymously. Set it, or drop the key entirely."
            )

    timeout_s = _DEFAULT_TIMEOUT_S
    if raw.get("timeout_s") is not None:
        timeout_s = _positive_number(raw["timeout_s"], name, "'timeout_s'", _MAX_TIMEOUT_S)

    max_bytes = _DEFAULT_MAX_RESPONSE_BYTES
    if raw.get("max_response_bytes") is not None:
        max_bytes = int(
            _positive_number(
                raw["max_response_bytes"],
                name,
                "'max_response_bytes'",
                _MAX_RESPONSE_BYTES_CEILING,
            )
        )

    return MCPServerConfig(
        name=name,
        url=url,
        transport="streamable-http",
        auth_token=auth_token,
        timeout_s=timeout_s,
        max_response_bytes=max_bytes,
    )


def _sse_payloads(raw: bytes) -> list[str]:
    """Extract the ``data:`` payload of each SSE event in a bounded body."""
    out: list[str] = []
    for block in raw.decode("utf-8", "replace").split("\n\n"):
        lines = [ln[5:].lstrip() for ln in block.splitlines() if ln.startswith("data:")]
        if lines:
            out.append("\n".join(lines))
    return out


class _Session:
    """One lazily-initialised streamable-HTTP session against one server."""

    def __init__(self, config: MCPServerConfig):
        self._config = config
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None
        self._initialized = False
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._tools: list[dict] | None = None
        self._tools_at = 0.0

    # -- plumbing ---------------------------------------------------------

    @property
    def config(self) -> MCPServerConfig:
        return self._config

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            cfg = self._config
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    cfg.timeout_s, connect=min(_CONNECT_TIMEOUT_S, cfg.timeout_s)
                ),
                # Never follow a redirect: it would replay the Authorization
                # header at whatever host the server names.
                follow_redirects=False,
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": _PROTOCOL_VERSION,
        }
        if self._config.auth_token:
            headers["Authorization"] = f"Bearer {self._config.auth_token}"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def reset(self) -> None:
        """Forget the session so the next call re-initialises from scratch."""
        self._session_id = None
        self._initialized = False
        self._tools = None

    async def _read_bounded(self, resp: httpx.Response) -> bytes:
        cap = self._config.max_response_bytes
        declared = resp.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > cap:
            raise MCPResponseTooLargeError(
                f"server '{self._config.name}': response of {declared} bytes exceeds "
                f"the {cap}-byte cap"
            )
        buf = bytearray()
        async for chunk in resp.aiter_bytes():
            buf.extend(chunk)
            if len(buf) > cap:
                raise MCPResponseTooLargeError(
                    f"server '{self._config.name}': response exceeds the {cap}-byte cap"
                )
        return bytes(buf)

    async def _post(self, payload: dict, *, notification: bool = False) -> tuple[bytes, str] | None:
        name = self._config.name
        client = self._ensure_client()
        body = json.dumps(payload).encode()
        try:
            async with client.stream(
                "POST", self._config.url, content=body, headers=self._headers()
            ) as resp:
                session_id = resp.headers.get("mcp-session-id")
                if session_id:
                    self._session_id = session_id
                if resp.status_code == 404 and self._session_id:
                    # Expired/unknown session id — drop it so the next call re-inits.
                    self.reset()
                if resp.status_code >= 300:
                    # 3xx included: redirects are never followed (that would
                    # replay the Authorization header at a host of the server's
                    # choosing), so one is a failure, not a hop.
                    # Deliberately no URL and no response body: either can carry
                    # a credential the operator embedded in config.
                    raise MCPTransportError(
                        f"server '{name}': HTTP {resp.status_code} from MCP endpoint"
                    )
                if notification or resp.status_code == 204:
                    return None
                raw = await self._read_bounded(resp)
                return raw, resp.headers.get("content-type", "")
        except httpx.TimeoutException as exc:
            raise MCPTimeoutError(
                f"server '{name}': transport timeout ({type(exc).__name__})"
            ) from None
        except httpx.HTTPError as exc:
            raise MCPTransportError(
                f"server '{name}': transport failure ({type(exc).__name__})"
            ) from None

    def _decode(self, raw: bytes, content_type: str, request_id: int) -> dict:
        name = self._config.name
        if "text/event-stream" in content_type.lower():
            candidates = _sse_payloads(raw)
        else:
            candidates = [raw.decode("utf-8", "replace")]

        message: dict | None = None
        for text in candidates:
            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            if isinstance(parsed, dict) and parsed.get("id") == request_id:
                message = parsed
                break
        if message is None:
            raise MCPProtocolError(
                f"server '{name}': no JSON-RPC response matching request id {request_id}"
            )
        if message.get("jsonrpc") != "2.0":
            raise MCPProtocolError(f"server '{name}': response is not JSON-RPC 2.0")
        if message.get("error") is not None:
            err = message["error"]
            code = err.get("code") if isinstance(err, dict) else None
            detail = str(err.get("message") if isinstance(err, dict) else err)[:_ERROR_CHARS]
            raise MCPServerError(f"server '{name}': JSON-RPC error {code}: {detail}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise MCPProtocolError(f"server '{name}': 'result' is missing or not an object")
        return result

    async def _rpc(self, method: str, params: dict) -> dict:
        self._next_id += 1
        request_id = self._next_id
        response = await self._post(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        if response is None:
            raise MCPProtocolError(f"server '{self._config.name}': empty response to {method}")
        raw, content_type = response
        return self._decode(raw, content_type, request_id)

    async def _ensure_initialized(self) -> None:
        async with self._lock:
            if self._initialized:
                return
            self._session_id = None
            result = await self._rpc(
                "initialize",
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": _CLIENT_NAME, "version": _CLIENT_VERSION},
                },
            )
            if not isinstance(result.get("protocolVersion"), str):
                raise MCPProtocolError(
                    f"server '{self._config.name}': initialize result has no protocolVersion"
                )
            await self._post(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}, notification=True
            )
            self._initialized = True
            logger.info(
                "mcp_session_initialized",
                server=self._config.name,
                protocol=result.get("protocolVersion"),
            )

    # -- operations -------------------------------------------------------

    async def list_tools(self, *, refresh: bool = False) -> list[dict]:
        now = time.monotonic()
        if not refresh and self._tools is not None and now - self._tools_at < _TOOLS_CACHE_TTL_S:
            return self._tools
        await self._ensure_initialized()
        result = await self._rpc("tools/list", {})
        raw_tools = result.get("tools")
        if not isinstance(raw_tools, list):
            raise MCPProtocolError(
                f"server '{self._config.name}': tools/list did not return a 'tools' array"
            )
        tools: list[dict] = []
        for entry in raw_tools:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                raise MCPProtocolError(
                    f"server '{self._config.name}': malformed entry in tools/list"
                )
            if not entry["name"]:
                raise MCPProtocolError(f"server '{self._config.name}': tool with an empty name")
            schema = entry.get("inputSchema")
            tools.append(
                {
                    "name": entry["name"],
                    "description": str(entry.get("description") or "")[:1000],
                    "inputSchema": schema if isinstance(schema, dict) else {},
                }
            )
        self._tools = tools
        self._tools_at = now
        return tools

    async def call_tool(self, tool: str, args: dict) -> dict:
        await self._ensure_initialized()
        return await self._rpc("tools/call", {"name": tool, "arguments": args or {}})

    async def close(self) -> None:
        client, self._client = self._client, None
        self.reset()
        if client is not None:
            await client.aclose()


class MCPManager:
    """Owns the configured MCP servers and their lazy sessions.

    Constructing the manager contacts nothing. A malformed server entry is
    rejected here — logged at ERROR, excluded from :meth:`list_servers` as
    usable, and turned into an immediate :class:`MCPConfigError` for anyone who
    tries to use it — rather than degrading to a confusing ``None`` downstream
    (issue #205) or crashing the whole API over one typo.
    """

    def __init__(self, server_configs: dict[str, Any] | None = None, *, enabled: bool = False):
        self._enabled = bool(enabled)
        self._configs: dict[str, MCPServerConfig] = {}
        self._errors: dict[str, str] = {}
        self._sessions: dict[str, _Session] = {}

        for name, raw in (server_configs or {}).items():
            try:
                self._configs[name] = parse_server_config(str(name), raw)
            except MCPConfigError as exc:
                self._errors[str(name)] = str(exc)
                logger.error("mcp_server_config_rejected", server=str(name), error=str(exc))

        if not self._enabled:
            logger.info(
                "mcp_disabled",
                configured=len(self._configs) + len(self._errors),
                reason="mcp_enabled is off — no MCP server will be contacted",
            )
        elif not self._configs:
            logger.warning("mcp_enabled_but_no_usable_servers", rejected=sorted(self._errors))
        else:
            logger.info("mcp_ready", servers=sorted(self._configs), rejected=sorted(self._errors))

    # -- introspection ----------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    def list_servers(self) -> list[dict]:
        """Configured servers and their config health. Never returns a token."""
        out = [
            {
                "name": cfg.name,
                "transport": cfg.transport,
                "url": cfg.url,
                "auth": bool(cfg.auth_token),
                "timeout_s": cfg.timeout_s,
                "max_response_bytes": cfg.max_response_bytes,
                "usable": True,
                "error": None,
            }
            for cfg in self._configs.values()
        ]
        out.extend(
            {
                "name": name,
                "transport": None,
                "url": None,
                "auth": False,
                "timeout_s": None,
                "max_response_bytes": None,
                "usable": False,
                "error": error,
            }
            for name, error in self._errors.items()
        )
        return sorted(out, key=lambda row: row["name"])

    def ensure_enabled(self) -> None:
        if not self._enabled:
            raise MCPDisabledError(
                "MCP client is disabled — enable 'MCP client (external tool servers)' "
                "under Integrations → Features and restart Core."
            )

    def _session(self, server: str) -> _Session:
        self.ensure_enabled()
        if server in self._errors:
            raise MCPConfigError(self._errors[server])
        config = self._configs.get(server)
        if config is None:
            raise MCPUnknownServerError(f"MCP server '{server}' not configured")
        session = self._sessions.get(server)
        if session is None:
            session = _Session(config)
            self._sessions[server] = session
        return session

    # -- operations -------------------------------------------------------

    async def _bounded(self, session: _Session, coro_factory, *, op: str, server: str) -> Any:
        """Run one operation under the server's total-call budget.

        Any transport-level failure resets the session so a hostile or restarted
        server cannot wedge every subsequent call to it.
        """
        try:
            return await asyncio.wait_for(coro_factory(), timeout=session.config.timeout_s)
        except TimeoutError:
            session.reset()
            raise MCPTimeoutError(
                f"server '{server}': {op} exceeded the {session.config.timeout_s:g}s budget"
            ) from None
        except (MCPTransportError, MCPTimeoutError, MCPProtocolError, MCPResponseTooLargeError):
            session.reset()
            raise

    async def list_tools(self, server: str, *, refresh: bool = False) -> list[dict]:
        """Discover a server's tools. Discovery only — nothing is executed."""
        session = self._session(server)
        tools = await self._bounded(
            session, lambda: session.list_tools(refresh=refresh), op="tools/list", server=server
        )
        logger.info("mcp_tools_listed", server=server, count=len(tools))
        return tools

    async def call_tool(self, server: str, tool: str, args: dict) -> dict:
        session = self._session(server)
        logger.info("mcp_call", server=server, tool=tool, arg_keys=sorted((args or {}).keys()))
        return await self._bounded(
            session, lambda: session.call_tool(tool, args), op=f"tools/call {tool}", server=server
        )

    async def close(self) -> None:
        """Tear down every open session. Never raises."""
        sessions, self._sessions = self._sessions, {}
        for name, session in sessions.items():
            try:
                await session.close()
            except Exception as exc:  # noqa: BLE001 — shutdown must not raise
                logger.warning("mcp_session_close_failed", server=name, error=type(exc).__name__)
