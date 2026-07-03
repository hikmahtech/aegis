"""Shared plumbing for HTTP-based connectors.

Every HTTP connector (vercel, todoist, knowledge, finance, search) needs
the same three things: a lazily-created pooled `httpx.AsyncClient`, a
`close()` for app shutdown, and a best-effort `connector_calls` audit row.
This base collapses that boilerplate. Subclasses set `connector_name` and
implement `_build_client()` with their own base_url / headers / auth /
transport — the only part that genuinely differs between connectors.
"""

from __future__ import annotations

import httpx
import structlog

logger = structlog.get_logger()


class HTTPConnector:
    """Lazy pooled client + observability for HTTP connectors."""

    # Subclasses override so connector_calls rows are attributed correctly.
    connector_name: str = ""

    def __init__(self, *, timeout: float = 30.0, db_pool=None) -> None:
        self._timeout = timeout
        self._db_pool = db_pool
        self._client: httpx.AsyncClient | None = None

    def _build_client(self) -> httpx.AsyncClient:
        """Construct the pooled client. Subclasses MUST override."""
        raise NotImplementedError

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = self._build_client()
        return self._client

    async def close(self) -> None:
        """Close the pooled client (called at app shutdown)."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def _record(
        self, action: str, status: str, latency_ms: int, error: str | None = None
    ) -> None:
        """Best-effort connector_calls audit row. Never raises."""
        if not self._db_pool:
            return
        try:
            from aegis.observability import record_connector_call

            await record_connector_call(
                self._db_pool,
                connector=self.connector_name,
                action=action,
                status=status,
                latency_ms=latency_ms,
                error=error,
            )
        except Exception as exc:
            logger.warning(
                "connector_observability_failed",
                connector=self.connector_name,
                action=action,
                status=status,
                error=str(exc)[:200],
            )
