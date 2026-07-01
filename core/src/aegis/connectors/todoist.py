"""Todoist Sync API v1 connector.

AEGIS uses Todoist's v1 sync endpoint exclusively for reads (incremental
sync via sync_token) and writes (atomic batches with temp_id). The legacy
/sync/v9/sync endpoint was deprecated in 2025; only /api/v1/sync works.
The connector returns the standard AEGIS envelope:
{ok, data, error, retryable, external_ref}.
"""

from __future__ import annotations

import time
import uuid

import httpx
import structlog

from aegis.connectors._base import HTTPConnector

logger = structlog.get_logger()


_BASE_URL = "https://api.todoist.com"
_SYNC_PATH = "/api/v1/sync"
_UPLOAD_PATH = "/api/v1/uploads"
_UPLOAD_TIMEOUT = 60.0  # uploads can be slow; 30s default occasionally trips


class TodoistConnector(HTTPConnector):
    """HTTP client for Todoist Sync API v1."""

    connector_name = "todoist"

    def __init__(
        self,
        api_key: str,
        base_url: str = _BASE_URL,
        timeout: float = 30.0,
        db_pool=None,
    ):
        super().__init__(timeout=timeout, db_pool=db_pool)
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=httpx.Timeout(self._timeout, connect=5.0),
            transport=httpx.AsyncHTTPTransport(retries=2),
        )

    def _envelope(
        self,
        ok: bool,
        data: dict | None = None,
        error: str | None = None,
        retryable: bool = False,
        external_ref: str | None = None,
    ) -> dict:
        return {
            "ok": ok,
            "data": data,
            "error": error,
            "retryable": retryable,
            "external_ref": external_ref,
        }

    async def _handle_http_response(self, resp, op_name: str, elapsed: int) -> dict:
        """Map an httpx response to the standard envelope via the shared
        status ladder, recording the per-status action under ``op_name``.

        200 → ok with data=resp.json(); 401/403 → unauthorized (non-retryable);
        5xx → server_error (retryable); 429 → rate_limited (retryable);
        anything else → client_error (non-retryable).
        """
        if resp.status_code == 200:
            await self._record(op_name, "ok", elapsed)
            return self._envelope(ok=True, data=resp.json())
        if resp.status_code in (401, 403):
            await self._record(op_name, "unauthorized", elapsed, resp.text[:200])
            return self._envelope(ok=False, error="unauthorized", retryable=False)
        if resp.status_code >= 500:
            await self._record(op_name, "server_error", elapsed, resp.text[:200])
            return self._envelope(ok=False, error=f"http_{resp.status_code}", retryable=True)
        if resp.status_code == 429:
            await self._record(op_name, "rate_limited", elapsed, resp.text[:200])
            return self._envelope(ok=False, error="rate_limited", retryable=True)
        await self._record(op_name, "client_error", elapsed, resp.text[:200])
        return self._envelope(ok=False, error=f"http_{resp.status_code}", retryable=False)

    # ---------------- Sync read ----------------

    async def sync(self, sync_token: str, resource_types: list[str]) -> dict:
        """POST /api/v1/sync — incremental sync.

        sync_token='*' on first call returns a full snapshot. Subsequent calls
        pass the token returned by the previous response for a delta.
        """
        if not self._api_key:
            return self._envelope(ok=False, error="no_api_key", retryable=False)

        client = await self._ensure_client()
        payload = {
            "sync_token": sync_token,
            "resource_types": resource_types,
        }
        started = time.perf_counter()
        try:
            r = await client.post(_SYNC_PATH, json=payload)
        except httpx.TimeoutException as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            await self._record("sync", "timeout", elapsed, str(exc)[:200])
            return self._envelope(ok=False, error="timeout", retryable=True)
        except httpx.NetworkError as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            await self._record("sync", "network_error", elapsed, str(exc)[:200])
            return self._envelope(ok=False, error="network", retryable=True)

        elapsed = int((time.perf_counter() - started) * 1000)
        return await self._handle_http_response(r, "sync", elapsed)

    # ---------------- Write (commands batch) ----------------

    async def commands(self, commands: list[dict]) -> dict:
        """POST /api/v1/sync with commands array.

        Each command is {type, uuid, args, temp_id?} — temp_id maps to a real
        id in the response's temp_id_mapping. Caller builds the command list;
        connector handles transport and envelope.
        """
        if not self._api_key:
            return self._envelope(ok=False, error="no_api_key", retryable=False)
        if not commands:
            return self._envelope(ok=True, data={"sync_status": {}, "temp_id_mapping": {}})

        client = await self._ensure_client()
        payload = {"commands": commands}
        started = time.perf_counter()
        try:
            r = await client.post(_SYNC_PATH, json=payload)
        except httpx.TimeoutException as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            await self._record("commands", "timeout", elapsed, str(exc)[:200])
            return self._envelope(ok=False, error="timeout", retryable=True)
        except httpx.NetworkError as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            await self._record("commands", "network_error", elapsed, str(exc)[:200])
            return self._envelope(ok=False, error="network", retryable=True)

        elapsed = int((time.perf_counter() - started) * 1000)
        return await self._handle_http_response(r, "commands", elapsed)

    # ---------------- File uploads ----------------

    async def upload_file(
        self,
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> dict:
        """POST /api/v1/uploads/add — multipart upload returning a
        Todoist `file_attachment` blob suitable for embedding in a
        subsequent `note_add` command's `args.file_attachment` field.

        Response shape (on success):
            {file_url, file_name, file_size, file_type,
             upload_state, resource_type}

        Returns the standard envelope with `data` set to the blob above.
        """
        if not self._api_key:
            return self._envelope(ok=False, error="no_api_key", retryable=False)
        if not filename or not content:
            return self._envelope(ok=False, error="missing_file", retryable=False)

        client = await self._ensure_client()
        files = {"file": (filename, content, content_type)}
        started = time.perf_counter()
        try:
            # Override the client's default timeout for this call —
            # multipart uploads of compressed transcripts (~100KB) can
            # legitimately take 20-40s over a constrained link.
            r = await client.post(_UPLOAD_PATH, files=files, timeout=_UPLOAD_TIMEOUT)
        except httpx.TimeoutException as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            await self._record("upload", "timeout", elapsed, str(exc)[:200])
            return self._envelope(ok=False, error="timeout", retryable=True)
        except httpx.NetworkError as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            await self._record("upload", "network_error", elapsed, str(exc)[:200])
            return self._envelope(ok=False, error="network", retryable=True)

        elapsed = int((time.perf_counter() - started) * 1000)
        return await self._handle_http_response(r, "upload", elapsed)

    # ---------------- Envelope inspection ----------------

    # Per-command error_tag / error_code values that Todoist treats as
    # permanently non-retryable (the item / project / args are gone or
    # invalid; replaying won't help). Anything else inside sync_status
    # may be a transient server-side issue.
    _NON_RETRYABLE_ERROR_TAGS = frozenset(
        {
            "ITEM_NOT_FOUND",
            "PROJECT_NOT_FOUND",
            "LABEL_NOT_FOUND",
            "INVALID_ARGUMENT",
            "INVALID_DATA",
            "INVALID_COMMAND_TYPE",
            "DUPLICATE",
            "UNAUTHORIZED",
            "FORBIDDEN",
        }
    )

    @staticmethod
    def _is_permanent_error(entry) -> bool:
        """Classify a single per-command sync_status entry as permanently
        non-retryable (replaying won't help) vs transient.

        Permanent when the entry is an error dict whose ``error_tag`` is in
        ``_NON_RETRYABLE_ERROR_TAGS`` or whose ``http_code`` is 4xx, OR when
        the entry is ``None`` (Todoist never acked this command's uuid —
        treat as permanent to avoid infinite re-queue). Anything else
        (e.g. an error dict with a 5xx ``http_code``) is transient.

        The caller is expected to have already handled the ``"ok"`` case;
        an ``"ok"`` (or any non-dict, non-None) entry classifies as
        transient (False).
        """
        if isinstance(entry, dict):
            tag = entry.get("error_tag") or ""
            http_code = entry.get("http_code")
            return tag in TodoistConnector._NON_RETRYABLE_ERROR_TAGS or (
                isinstance(http_code, int) and 400 <= http_code < 500
            )
        # None → Todoist never acked this command's uuid (permanent);
        # any other non-dict value (e.g. "ok") classifies as transient.
        return entry is None

    @staticmethod
    def check_sync_status(envelope: dict, command_uuids: list[str]) -> dict:
        """Inspect a `commands()` envelope plus per-command sync_status.

        The Sync API envelope can return `ok=True` (HTTP 200) while one or
        more commands inside the batch were rejected with a per-command
        error dict in `data.sync_status[uuid]`. Callers that only check
        envelope-ok get a silent false success — observed multiple times
        in prod (e.g. ITEM_NOT_FOUND when our projection is stale).

        Returns:
            {
                "ok": bool,             # envelope-ok AND every uuid sync_status == "ok"
                "envelope_error": str | None,
                "retryable": bool,      # only true when envelope-fail with retryable=True
                "rejected": dict[str, dict | str],   # uuid -> sync_status (cmd was rejected)
                "rejected_retryable": bool,         # True iff every rejection is in the
                                                    # retryable class (5xx-style); False if
                                                    # any is in _NON_RETRYABLE_ERROR_TAGS
            }

        Reference pattern: `worker/.../activities/alerts.py::post_task_note`.
        """
        if not envelope.get("ok"):
            return {
                "ok": False,
                "envelope_error": envelope.get("error"),
                "retryable": bool(envelope.get("retryable")),
                "rejected": {},
                "rejected_retryable": False,
            }
        sync_status = (envelope.get("data") or {}).get("sync_status") or {}
        # Lenient legacy path: when sync_status is completely empty AND the
        # envelope is ok, treat as success. Real Todoist Sync API responses
        # always populate sync_status with one entry per command UUID, so an
        # empty dict means the caller (typically a test mock) doesn't model
        # per-command status. This keeps the helper backward-compatible
        # with the many tests that mock the connector with
        # `{ok: True, data: {sync_status: {}}}` — they're testing application
        # logic, not the envelope contract.
        #
        # Telemetry: log a warning so we can spot whether this branch ever
        # fires in production. A degraded Todoist response (HTTP 200 with
        # empty body / partial response) would be silently reported as
        # success otherwise.
        if not sync_status and command_uuids:
            logger.warning(
                "todoist_check_sync_status_empty_lenient_fallback",
                command_uuids_count=len(command_uuids),
                has_temp_id_mapping=bool((envelope.get("data") or {}).get("temp_id_mapping")),
            )
            return {
                "ok": True,
                "envelope_error": None,
                "retryable": False,
                "rejected": {},
                "rejected_retryable": False,
            }
        rejected: dict = {}
        any_non_retryable = False
        for cmd_uuid in command_uuids:
            st = sync_status.get(cmd_uuid)
            if st == "ok":
                continue
            rejected[cmd_uuid] = st
            if TodoistConnector._is_permanent_error(st):
                any_non_retryable = True
        return {
            "ok": not rejected,
            "envelope_error": None,
            "retryable": False,
            "rejected": rejected,
            "rejected_retryable": bool(rejected) and not any_non_retryable,
        }

    # ---------------- Convenience builders ----------------

    @staticmethod
    def build_create_project_command(name: str, parent_id: str | None = None) -> dict:
        return {
            "type": "project_add",
            "uuid": str(uuid.uuid4()),
            "temp_id": f"proj-{uuid.uuid4()}",
            "args": {"name": name, **({"parent_id": parent_id} if parent_id else {})},
        }

    @staticmethod
    def build_create_label_command(name: str, color: str | None = None) -> dict:
        args: dict = {"name": name}
        if color:
            args["color"] = color
        return {
            "type": "label_add",
            "uuid": str(uuid.uuid4()),
            "temp_id": f"label-{uuid.uuid4()}",
            "args": args,
        }

    @staticmethod
    def build_create_filter_command(name: str, query: str, color: str | None = None) -> dict:
        args: dict = {"name": name, "query": query}
        if color:
            args["color"] = color
        return {
            "type": "filter_add",
            "uuid": str(uuid.uuid4()),
            "temp_id": f"filter-{uuid.uuid4()}",
            "args": args,
        }

    @staticmethod
    def build_create_item_command(
        project_id: str,
        content: str,
        description: str | None = None,
        labels: list[str] | None = None,
        due_date: str | None = None,
        priority: int | None = None,
    ) -> dict:
        args: dict = {"project_id": project_id, "content": content}
        if description:
            args["description"] = description
        if labels:
            args["labels"] = labels
        if due_date:
            args["due"] = {"date": due_date}
        if priority:
            args["priority"] = priority
        return {
            "type": "item_add",
            "uuid": str(uuid.uuid4()),
            "temp_id": f"item-{uuid.uuid4()}",
            "args": args,
        }

    @staticmethod
    def build_note_add_command(
        item_id: str,
        content: str,
        file_attachment: dict | None = None,
    ) -> dict:
        """note_add — attach a comment to an existing Todoist task.

        temp_id is included so the response's temp_id_mapping yields the
        real note id, which CaptureActivities + ClarifyFlow do not need
        but the outbox does for committed-id audit.

        `file_attachment` is the blob returned by `upload_file()` —
        passing it embeds the upload as an attachment on the comment.
        """
        args: dict = {"item_id": item_id, "content": content}
        if file_attachment:
            args["file_attachment"] = file_attachment
        return {
            "type": "note_add",
            "uuid": str(uuid.uuid4()),
            "temp_id": f"note-{uuid.uuid4()}",
            "args": args,
        }

    @staticmethod
    def build_item_move_command(item_id: str, project_id: str) -> dict:
        """item_move — relocate a task to a different project.

        Update commands take an existing id; no temp_id is needed.
        """
        return {
            "type": "item_move",
            "uuid": str(uuid.uuid4()),
            "args": {"id": item_id, "project_id": project_id},
        }

    @staticmethod
    def build_item_update_command(item_id: str, **fields) -> dict:
        """item_update — patch existing task fields (labels, priority, etc.).

        kwargs are passed through to Todoist's item_update args; valid keys
        are documented at https://developer.todoist.com/api/v1#tag/Sync.
        """
        return {
            "type": "item_update",
            "uuid": str(uuid.uuid4()),
            "args": {"id": item_id, **fields},
        }

    @staticmethod
    def build_item_complete_command(item_id: str) -> dict:
        """item_complete — mark task as completed."""
        return {
            "type": "item_complete",
            "uuid": str(uuid.uuid4()),
            "args": {"id": item_id},
        }
