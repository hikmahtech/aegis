"""ClickHouse connector — read-only access to trading system data."""

from __future__ import annotations

import json
import time

import httpx
import structlog

from aegis.connectors._base import HTTPConnector

logger = structlog.get_logger()


class ClickHouseConnector(HTTPConnector):
    """HTTP client for ClickHouse queries (read-only)."""

    connector_name = "clickhouse"

    def __init__(
        self,
        host: str,
        port: int = 8123,
        user: str = "",
        password: str = "",
        database: str = "trading_system",
        db_pool=None,
    ):
        super().__init__(db_pool=db_pool)
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._database = database

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=f"http://{self._host}:{self._port}",
            auth=(self._user, self._password) if self._user else None,
            timeout=httpx.Timeout(10.0, connect=5.0),
        )

    async def query(self, sql: str, params: dict | None = None) -> list[dict]:
        """Execute a SELECT query and return rows as list of dicts.

        Uses ClickHouse HTTP interface with JSONEachRow format.
        Params are passed as query parameters (param_name=value).
        """
        client = await self._ensure_client()
        query_params: dict = {
            "database": self._database,
            "default_format": "JSONEachRow",
            "query": sql,
        }
        if params:
            for key, value in params.items():
                query_params[f"param_{key}"] = str(value)

        t0 = time.monotonic()
        try:
            resp = await client.get("/", params=query_params)
            if resp.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}: {resp.text}",
                    request=httpx.Request("GET", "/"),
                    response=resp,
                )
            latency = int((time.monotonic() - t0) * 1000)
            await self._record("query", "ok", latency)

            text = resp.text.strip()
            if not text:
                return []
            rows = [json.loads(line) for line in text.split("\n") if line.strip()]
            return rows
        except Exception as exc:
            latency = int((time.monotonic() - t0) * 1000)
            await self._record("query", "error", latency, str(exc))
            raise

    async def health(self) -> bool:
        """Check if ClickHouse is reachable."""
        try:
            client = await self._ensure_client()
            resp = await client.get("/ping")
            return resp.status_code == 200
        except Exception:
            return False
