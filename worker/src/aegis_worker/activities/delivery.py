"""Comms delivery activities (HTTP client for the aegis-comms delivery server)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
from temporalio import activity

_logger = structlog.get_logger()


async def safe_send_message(delivery: Any, *, agent_id: str, message: str, log_event: str) -> None:
    """Best-effort wrapper around `DeliveryActivities.send_message` for
    fire-and-forget notification call sites. Logs *both* raised exceptions
    and `{ok: false}` dict returns under `log_event` so a future failure
    can't hide the way the dict-vs-str 422 bug did pre-PR #257.

    Does not raise. Suitable for notify_drift / cert / backup / renewal
    paths where we'd rather drop the alert than poison the activity.

    Notification budget (Phase 5): each proactive push is recorded; when the
    budget is enabled and the daily cap is hit, the push is deferred (skipped —
    the daily digest is the carrier) instead of sent.

    Web channel (Phase C): proactive FYIs have no external push target — the
    admin surfaces (inbox / feeds) are the destination — so this no-ops.
    """
    if getattr(delivery, "channel", "web") != "slack":
        return
    pool = getattr(delivery, "db_pool", None)
    if pool is not None:
        try:
            from aegis.services.notifications import record_notification, should_send

            allow, today = await should_send(
                pool,
                enabled=getattr(delivery, "budget_enabled", False),
                daily_budget=getattr(delivery, "daily_budget", 8),
            )
            if not allow:
                _logger.info(
                    "notification_deferred", log_event=log_event, agent_id=agent_id, today=today
                )
                await record_notification(pool, agent_id, log_event, sent=False)
                return
        except Exception as exc:  # noqa: BLE001 — budget must never block delivery
            _logger.warning("notification_budget_check_failed", error=str(exc)[:200])

    try:
        result = await delivery.send_message(agent_id=agent_id, message=message, chat_id=0)
    except Exception as exc:  # noqa: BLE001 — boundary, must not propagate
        _logger.warning(log_event, error=str(exc)[:200], reason="raised")
        return
    if isinstance(result, dict) and not result.get("ok"):
        _logger.warning(
            log_event,
            error=str(result.get("error", "ok=false"))[:200],
            reason="ok_false",
        )
    if pool is not None:
        try:
            from aegis.services.notifications import record_notification

            await record_notification(pool, agent_id, log_event, sent=True)
        except Exception:  # noqa: BLE001 — recording is best-effort
            pass


@dataclass
class DeliveryActivities:
    """Activities for delivering messages via the comms service.

    Uses a pooled httpx.AsyncClient across all activity invocations. Delivery
    fires on every workflow notification (task result, triage item, alert,
    decision, briefing), so a per-call client was the hottest source of
    avoidable TCP handshakes.
    """

    comms_url: str = ""
    api_key: str = ""
    tts_enabled: bool = False
    db_pool: Any = None  # for the notification budget (Phase 5)
    budget_enabled: bool = False
    daily_budget: int = 8
    channel: str = "web"  # active comms channel (Phase C); "web" = admin inbox only
    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.comms_url,
                headers={"X-API-Key": self.api_key} if self.api_key else {},
                # 30s covers both the text-message (15s original) and the
                # document-attachment (30s original) paths.
                timeout=httpx.Timeout(30.0, connect=5.0),
            )
        return self._client

    @activity.defn
    async def send_message(
        self, agent_id: str, message: str, chat_id: int = 0, keyboard: dict | None = None
    ) -> dict:
        """Send a message to an agent's channel."""
        if not self.comms_url:
            activity.logger.warning("comms_url_not_configured")
            return {"ok": False, "error": "comms_url not configured"}

        body: dict = {"text": message, "chat_id": chat_id, "agent_id": agent_id}
        if keyboard:
            body["reply_markup"] = keyboard

        client = self._ensure_client()
        resp = await client.post("/api/deliver/message", json=body)
        return resp.json()

    @activity.defn
    async def send_voice(self, agent_id: str, text: str) -> dict:
        """Post a per-persona voice note (additive to the text reply).

        Global kill switch: returns `{ok:False, skipped:'tts_disabled'}` without
        any network call unless `tts_enabled` (AEGIS_TTS_ENABLED) is set. The
        per-flow opt-in is simply that only specific flows call this activity.
        Best-effort — never the reason a flow fails.
        """
        if not self.tts_enabled:
            return {"ok": False, "skipped": "tts_disabled"}
        if not self.comms_url:
            return {"ok": False, "error": "comms_url not configured"}
        try:
            client = self._ensure_client()
            resp = await client.post(
                "/api/deliver/voice", json={"text": text, "agent_id": agent_id}
            )
            return resp.json()
        except Exception as exc:  # noqa: BLE001 — voice is additive, never fatal
            activity.logger.warning("send_voice_failed: %s", str(exc)[:200])
            return {"ok": False, "error": str(exc)[:200]}

    @activity.defn
    async def send_document(
        self,
        agent_id: str,
        documents: list[dict],
        caption: str = "",
        chat_id: int = 0,
        keyboard: dict | None = None,
    ) -> dict:
        """Send one or more markdown document attachments to an agent's topic.

        Each document dict must have keys: filename, content.
        Caption and keyboard (inline buttons) attach to the first document only.
        """
        if not self.comms_url:
            activity.logger.warning("comms_url_not_configured")
            return {"ok": False, "error": "comms_url not configured"}

        body: dict = {
            "documents": documents,
            "caption": caption,
            "chat_id": chat_id,
            "agent_id": agent_id,
        }
        if keyboard:
            body["reply_markup"] = keyboard

        client = self._ensure_client()
        resp = await client.post("/api/deliver/document", json=body)
        return resp.json()

    @activity.defn
    async def send_system_event(self, message: str, chat_id: int = 0) -> dict:
        """Send a system event to the General topic (workflow lifecycle, errors, etc.)."""
        if not self.comms_url:
            activity.logger.warning("comms_url_not_configured")
            return {"ok": False, "error": "comms_url not configured"}

        client = self._ensure_client()
        resp = await client.post(
            "/api/deliver/message",
            json={"text": message, "chat_id": chat_id, "system_event": True},
        )
        return resp.json()

    @activity.defn
    async def send_interaction_card(
        self,
        interaction_id: str,
        agent_id: str,
        kind: str,
        prompt: str,
        options: dict | None,
        allow_hint: bool = False,
    ) -> dict:
        """Dispatch a channel-neutral interaction card to the comms service.

        Keyboard/Block-Kit rendering lives in the comms package (the active
        ChannelAdapter renders the card for its channel). This activity just
        POSTs the neutral spec — `{interaction_id, agent_id, kind, prompt,
        options}` — to `/api/deliver/card`.

        Prompts pass through as-is (the channel applies its own markup);
        callers escape user-controlled substrings per project convention. The
        adapter routes to the agent's channel (no chat/topic override).
        """
        if self.channel != "slack" or not self.comms_url:
            # Web channel (the OSS default): the interaction row IS the delivery —
            # it shows up in the admin inbox and is resolved there. Mark it
            # delivered with a web ref so the DeliveryWatchdog stays quiet.
            return {"ok": True, "delivery_ref": {"adapter": "web"}}

        body: dict = {
            "interaction_id": interaction_id,
            "agent_id": agent_id,
            "kind": kind,
            "prompt": prompt or "",
            "options": options,
            "allow_hint": allow_hint,
        }

        client = self._ensure_client()
        resp = await client.post("/api/deliver/card", json=body)
        return resp.json()

    async def close(self) -> None:
        """Close the pooled HTTP client (best-effort; process exit covers it)."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
