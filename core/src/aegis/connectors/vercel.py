"""Vercel REST connector — read-only project + deployment + build-log queries.

Backs the four `vercel_*` chat tools available to Pandora (`team_id`
defaults to the example team via settings).

API endpoints exercised here:
  * GET /v9/projects/{id_or_name}              — project metadata
  * GET /v6/deployments                         — deployment list w/ filters
  * GET /v13/deployments/{id}                   — single deployment
  * GET /v3/deployments/{id}/events?builds=1   — build event stream

NOTE: Vercel's REST API does NOT expose runtime function logs on this
plan; that surface is gated to the dashboard + Log Drains. If Pandora
ever needs runtime errors specifically, the path is to set up a Log
Drain → /api/webhooks/vercel-logs and surface them via a separate tool.
For now build_logs covers the "deploy errored, what broke?" case.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from aegis.connectors._base import HTTPConnector
from aegis.errors import error_text

logger = structlog.get_logger()

_BASE_URL = "https://api.vercel.com"
_VALID_STATES = {"BUILDING", "ERROR", "INITIALIZING", "QUEUED", "READY", "CANCELED"}


def _iso(epoch_ms: Any) -> str | None:
    """Format Vercel's epoch-ms timestamps as ISO-8601 UTC strings.

    Vercel returns epoch milliseconds for `created`, `ready`, `createdAt`, etc.
    Raw numbers confused gpt-oss:20b (it reported 2026 deploys as "2023-12-17"
    — date hallucination from the magnitude of the integer). Returning ISO
    strings removes the interpretation step from the LLM.
    """
    if epoch_ms is None:
        return None
    try:
        return (
            datetime.fromtimestamp(int(epoch_ms) / 1000, tz=UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class _Failed(dict):
    """What `_get` returns when a read did not succeed.

    A `dict`, so a caller hands it straight back to the chat tool as the
    `{"error": ...}` these methods have always returned; a distinct type, so
    "did this fail?" never has to guess from the presence of an `error` key
    in a body Vercel sent with a 200.
    """


class VercelConnector(HTTPConnector):
    """Async HTTP client for Vercel REST. Read-only — no deploy/redeploy actions."""

    connector_name = "vercel"

    def __init__(
        self,
        token: str,
        team_id: str = "",
        base_url: str = _BASE_URL,
        timeout: int = 30,
        db_pool=None,
    ):
        super().__init__(timeout=timeout, db_pool=db_pool)
        self._token = token
        self._team_id = team_id
        self._base_url = base_url.rstrip("/")

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=self._timeout,
        )

    def _team_params(self) -> dict[str, str]:
        return {"teamId": self._team_id} if self._team_id else {}

    async def _get(
        self, action: str, path: str, params: dict, *, not_found: dict | None = None
    ) -> Any:
        """One GET, through the ladder every read here shares.

        A transport error, the 404 the caller names (when it names one), any
        other non-2xx, else the decoded body — and a `connector_calls` row
        either way. Every failure comes back as a `_Failed`, so nothing in
        this connector raises at a chat tool, and a 200 body that happens to
        carry its own `error` key is never mistaken for one.
        """
        client = await self._ensure_client()
        start = time.monotonic()
        try:
            resp = await client.get(path, params=params)
        except httpx.HTTPError as exc:
            await self._record(
                action, "error", int((time.monotonic() - start) * 1000), error_text(exc, 500)
            )
            return _Failed({"error": f"http_error: {exc!s}"})
        latency_ms = int((time.monotonic() - start) * 1000)
        if not_found is not None and resp.status_code == 404:
            await self._record(action, "not_found", latency_ms)
            return _Failed(not_found)
        if not resp.is_success:
            await self._record(action, "error", latency_ms, resp.text[:500])
            return _Failed({"error": f"http_{resp.status_code}", "body": resp.text[:500]})
        body = resp.json()
        await self._record(action, "ok", latency_ms)
        return body

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    async def get_project(self, project_id_or_name: str) -> dict[str, Any]:
        """Fetch a project by id (`prj_...`) or name. Returns the JSON body."""
        if not self._token:
            return {"error": "vercel_token_not_configured"}
        if not project_id_or_name:
            return {"error": "project_id_or_name is required"}
        return await self._get(
            "get_project",
            f"/v9/projects/{project_id_or_name}",
            self._team_params(),
            not_found={"error": "project_not_found", "project": project_id_or_name},
        )

    async def list_deployments(
        self,
        project_id_or_name: str,
        *,
        limit: int = 10,
        since_hours: int | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        """List deployments with optional time + state filters.

        `since_hours` filters to deploys created within the last N hours.
        `state` is one of READY|ERROR|BUILDING|CANCELED|INITIALIZING|QUEUED
        (case-insensitive). Returns `{deployments: [...]}` or `{error: ...}`.
        """
        if not self._token:
            return {"error": "vercel_token_not_configured"}
        if not project_id_or_name:
            return {"error": "project_id_or_name is required"}
        if state and state.upper() not in _VALID_STATES:
            return {"error": f"invalid_state: {state}", "allowed": sorted(_VALID_STATES)}
        limit = max(1, min(int(limit), 100))

        params: dict[str, Any] = dict(self._team_params())
        # Vercel v6 deployments accepts `projectId` for ids, `app` for names.
        if project_id_or_name.startswith("prj_"):
            params["projectId"] = project_id_or_name
        else:
            params["app"] = project_id_or_name
        params["limit"] = limit
        if since_hours is not None and since_hours > 0:
            params["since"] = int((time.time() - since_hours * 3600) * 1000)
        if state:
            params["state"] = state.upper()

        body = await self._get("list_deployments", "/v6/deployments", params)
        if isinstance(body, _Failed):
            return body
        # Trim to the fields actually useful in chat — full body is noisy.
        # Timestamps go out as ISO-8601 UTC (epoch ms confused gpt-oss).
        trimmed = [
            {
                "uid": d.get("uid"),
                "name": d.get("name"),
                "url": d.get("url"),
                "state": d.get("state") or d.get("readyState"),
                "created_at": _iso(d.get("created")),
                "ready_at": _iso(d.get("ready")),
                "creator": (d.get("creator") or {}).get("username"),
                "target": d.get("target"),
                "meta_branch": (d.get("meta") or {}).get("githubCommitRef"),
                "meta_sha": (d.get("meta") or {}).get("githubCommitSha", "")[:7] or None,
            }
            for d in body.get("deployments", [])
        ]
        return {"deployments": trimmed, "count": len(trimmed)}

    async def get_deployment(self, deployment_id: str) -> dict[str, Any]:
        """Single deployment detail including errorMessage if it ERROR'd."""
        if not self._token:
            return {"error": "vercel_token_not_configured"}
        if not deployment_id:
            return {"error": "deployment_id is required"}
        body = await self._get(
            "get_deployment",
            f"/v13/deployments/{deployment_id}",
            self._team_params(),
            not_found={"error": "deployment_not_found", "id": deployment_id},
        )
        if isinstance(body, _Failed):
            return body
        # Surface just the fields Pandora needs for triage — full body is huge.
        # Timestamps go out as ISO-8601 UTC (see _iso).
        return {
            "id": body.get("id") or body.get("uid"),
            "name": body.get("name"),
            "url": body.get("url"),
            "readyState": body.get("readyState") or body.get("state"),
            "target": body.get("target"),
            "created_at": _iso(body.get("createdAt") or body.get("created")),
            "buildErrorMessage": body.get("buildErrorMessage"),
            "errorMessage": body.get("errorMessage"),
            "errorCode": body.get("errorCode"),
            "errorStep": body.get("errorStep"),
            "creator": (body.get("creator") or {}).get("username"),
            "meta_branch": (body.get("meta") or {}).get("githubCommitRef"),
            "meta_sha": (body.get("meta") or {}).get("githubCommitSha", "")[:7] or None,
            "meta_message": (body.get("meta") or {}).get("githubCommitMessage"),
        }

    async def get_build_logs(
        self,
        deployment_id: str,
        *,
        limit: int = 100,
        errors_only: bool = False,
    ) -> dict[str, Any]:
        """Build events for a deployment (newest first).

        `errors_only` post-filters to events whose `type == 'stderr'` —
        the typical signal for "what broke during the build". The full
        Vercel events endpoint returns interleaved stdout/stderr/error
        records per build step.
        """
        if not self._token:
            return {"error": "vercel_token_not_configured"}
        if not deployment_id:
            return {"error": "deployment_id is required"}
        limit = max(1, min(int(limit), 1000))
        params = dict(self._team_params())
        params.update(
            {"builds": "1", "direction": "backward", "limit": limit, "follow": "0"}
        )
        events = await self._get(
            "get_build_logs",
            f"/v3/deployments/{deployment_id}/events",
            params,
            not_found={"error": "deployment_not_found", "id": deployment_id},
        )
        if isinstance(events, _Failed):
            return events
        events = events or []
        if errors_only:
            events = [e for e in events if e.get("type") == "stderr"]
        # Trim each event to text + type + timestamp for token economy.
        # Timestamps go out as ISO-8601 UTC (see _iso).
        trimmed = [
            {
                "type": e.get("type"),
                "text": (e.get("text") or "").rstrip(),
                "created_at": _iso(e.get("created")),
            }
            for e in events
        ]
        return {"events": trimmed, "count": len(trimmed), "errors_only": errors_only}
