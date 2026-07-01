"""AEGIS comms bot + delivery server (Slack).

Usage:
    python -m aegis_comms
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
import uvicorn
from fastapi import APIRouter, FastAPI, Header, HTTPException
from pydantic import BaseModel

from aegis_comms.adapters.base import CardSpec, DeliveryRef
from aegis_comms.adapters.slack import SlackAdapter
from aegis_comms.config import TelegramSettings

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Slack Socket Mode inbound liveness probe state
#
# Populated by _run_slack_socket_probe() which polls SlackAdapter.is_connected()
# every 60s alongside the delivery server.  Read by /api/health so the watchdog
# gets a real inbound-liveness signal for the single-connection Socket Mode
# hazard.
# ---------------------------------------------------------------------------

_SLACK_PROBE_INTERVAL = 60  # seconds between is_connected() polls
_PROBE_STALE_THRESHOLD = 180  # seconds — older than this → healthy=False


@dataclass
class _SlackSocketState:
    last_connected_at: float | None = field(default=None)  # time.monotonic() of last connected poll
    last_error: str | None = field(default=None)


_slack_socket_state = _SlackSocketState()


async def _slack_socket_probe_once(adapter) -> None:
    """One poll of the Slack Socket Mode connection; updates _slack_socket_state.

    Never raises. `is_connected()` returning None means the listener has not
    started yet — leave the watermark untouched so startup doesn't flap to down.
    """
    try:
        connected = await adapter.is_connected()
    except Exception as exc:  # noqa: BLE001 — probe is best-effort
        _slack_socket_state.last_error = str(exc)[:200]
        logger.warning("slack_socket_probe_failed", error=_slack_socket_state.last_error)
        return
    if connected is True:
        _slack_socket_state.last_connected_at = time.monotonic()
        _slack_socket_state.last_error = None
    elif connected is False:
        _slack_socket_state.last_error = "socket_not_connected"
    # connected is None → listener not started yet; hold the watermark.


async def _run_slack_socket_probe(adapter) -> None:
    """Background task: poll Slack Socket Mode liveness every 60s. Never crashes."""
    while True:
        await _slack_socket_probe_once(adapter)
        await asyncio.sleep(_SLACK_PROBE_INTERVAL)


async def _log_dispatch(
    settings: TelegramSettings,
    *,
    agent_id: str,
    content: str,
    send_result: dict,
    kind: str,
) -> None:
    """Fire-and-forget POST to core /api/chat/dispatches so every outbound
    Telegram message lands as a role='dispatch' row in chat_history. This
    closes a long-standing gap where briefings, interaction cards, alert
    notifications etc. were shown to the user but the chat had no record
    of them — so when the user replied referring to one, the assistant
    had no context.

    Never raises. Logging is observability, not delivery — a failure
    here must NOT cause the dispatch to look like it failed.
    """
    if not send_result.get("ok"):
        return
    if not settings.core_url:
        return
    # Prefer the channel-neutral delivery_ref; fall back to the legacy
    # top-level keys (which SendResult.to_response() still mirrors). Forward the
    # whole neutral ref block so a Slack dispatch is logged with its
    # {adapter,channel,ts} (the core 5a route stores it); keep the legacy
    # telegram keys when present so the Telegram path is unchanged.
    ref = send_result.get("delivery_ref") or {}
    payload = {
        "agent_id": agent_id,
        "topic_id": ref.get("topic_id", send_result.get("topic_id")),
        "chat_id": ref.get("chat_id", send_result.get("chat_id")),
        "message_id": ref.get("message_id", send_result.get("message_id")),
        "content": content,
        "kind": kind,
        "used_html": send_result.get("used_html", True),
    }
    if ref:
        payload["delivery_ref"] = ref
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"{settings.core_url}/api/chat/dispatches",
                json=payload,
                headers={"X-API-Key": settings.api_key} if settings.api_key else {},
                auth=(settings.admin_username, settings.admin_password)
                if settings.admin_username
                else None,
            )
    except Exception as exc:
        logger.warning("dispatch_log_failed", error=str(exc)[:200], kind=kind, agent=agent_id)


class DeliveryRequest(BaseModel):
    """Message delivery request from Core/Worker.

    The active channel adapter (Slack) routes by the agent's channel; there is
    no per-message chat/topic override.
    """

    text: str
    agent_id: str = "sebas"
    system_event: bool = False  # If true, send to General topic instead of agent topic
    parse_mode: str = "HTML"
    reply_markup: dict | None = None


class DocumentAttachment(BaseModel):
    """A single document attachment."""

    filename: str
    content: str  # UTF-8 text; binary content should be base64 (not currently used)


class DocumentDeliveryRequest(BaseModel):
    """Document delivery request — sends one or more text documents to a topic."""

    documents: list[DocumentAttachment]
    caption: str = ""
    agent_id: str = "sebas"
    reply_markup: dict | None = None


class VoiceDeliveryRequest(BaseModel):
    """Outbound per-persona voice-note request (called by worker flows).

    Additive to the text reply — the flow posts the text separately. The active
    adapter synthesizes `text` in the agent's ElevenLabs voice and uploads an mp3
    to the agent's channel.
    """

    text: str
    agent_id: str = "sebas"


class CardDeliveryRequest(BaseModel):
    """Channel-neutral interaction-card delivery request.

    The worker POSTs this instead of building a per-channel `reply_markup`;
    the active adapter renders the card for its channel and routes it to the
    agent's channel.
    """

    interaction_id: str
    agent_id: str = "sebas"
    kind: str
    prompt: str = ""
    options: dict | None = None
    allow_hint: bool = False


class DeleteRequest(BaseModel):
    """Channel-neutral message deletion request."""

    delivery_ref: dict


def create_delivery_app(adapter: SlackAdapter, settings: TelegramSettings) -> FastAPI:
    """Create FastAPI app for delivery endpoint + health.

    Routes outbound delivery through the `SlackAdapter` over a channel-neutral
    HTTP surface.
    """
    app = FastAPI(title="AEGIS Comms", version="2.0.0")
    app.state.adapter = adapter
    app.state.settings = settings

    router = APIRouter()

    @router.get("/api/health")
    async def health():
        now = time.monotonic()
        body: dict[str, Any] = {
            "status": "ok",
            "service": "aegis-comms",
            "version": "2.0.0",
            "channel": settings.channel,
        }

        # The generic `inbound` block carries the Socket Mode liveness signal.
        # No telegram_api block — its absence keeps an un-updated watchdog from
        # false-alarming on a never-run Telegram probe.
        last_ok_at = _slack_socket_state.last_connected_at
        if last_ok_at is None:
            last_ok_seconds_ago = None
            healthy = False
        else:
            last_ok_seconds_ago = int(now - last_ok_at)
            healthy = last_ok_seconds_ago < _PROBE_STALE_THRESHOLD
        body["inbound"] = {
            "channel": settings.channel,
            "healthy": healthy,
            "last_ok_seconds_ago": last_ok_seconds_ago,
            "last_error": _slack_socket_state.last_error,
        }
        return body

    @router.post("/api/deliver/telegram")
    async def deliver(
        req: DeliveryRequest,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """Deliver a message to an agent's channel (called by worker flows).

        Every successful send is mirrored into chat_history as a
        role='dispatch' row so the agent's chat context can see what the
        user has been shown. See `_log_dispatch` for the contract.
        """
        if settings.api_key and (not x_api_key or x_api_key != settings.api_key):
            raise HTTPException(401, "Invalid API key")

        if req.system_event:
            send_result = await adapter.send_system_event(text=req.text)
            result = send_result.to_response()
            await _log_dispatch(
                settings,
                agent_id="system",
                content=req.text,
                send_result=result,
                kind="system_event",
            )
            return {"ok": result.get("ok", False), "type": "system_event", **result}

        send_result = await adapter.send_message(
            agent_id=req.agent_id,
            text=req.text,
            target=None,
            reply_markup=req.reply_markup,
        )
        result = send_result.to_response()
        await _log_dispatch(
            settings,
            agent_id=req.agent_id,
            content=req.text,
            send_result=result,
            kind="deliver" if not req.reply_markup else "interaction_card",
        )
        return {"ok": result.get("ok", False), "agent_id": req.agent_id, **result}

    @router.post("/api/deliver/telegram/document")
    async def deliver_document(
        req: DocumentDeliveryRequest,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """Deliver one or more document attachments to an agent's channel."""
        if settings.api_key and (not x_api_key or x_api_key != settings.api_key):
            raise HTTPException(401, "Invalid API key")

        docs = [d.model_dump() for d in req.documents]
        send_result = await adapter.send_document(
            agent_id=req.agent_id,
            documents=docs,
            caption=req.caption,
            target=None,
            reply_markup=req.reply_markup,
        )
        ok = send_result.ok
        if ok and req.caption:
            await _log_dispatch(
                settings,
                agent_id=req.agent_id,
                content=req.caption,
                send_result=send_result.to_response(),
                kind="document",
            )
        return {"ok": ok, "agent_id": req.agent_id, "count": len(docs)}

    @router.post("/api/deliver/voice")
    async def deliver_voice(
        req: VoiceDeliveryRequest,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """Synthesize + upload a per-persona voice note to an agent's channel.

        Best-effort + additive: the worker already posted the text. A no-op
        (ok=False) when the agent has no voice_id or ElevenLabs isn't configured.
        """
        if settings.api_key and (not x_api_key or x_api_key != settings.api_key):
            raise HTTPException(401, "Invalid API key")
        send_result = await adapter.send_voice(agent_id=req.agent_id, text=req.text)
        result = send_result.to_response()
        return {"ok": result.get("ok", False), "agent_id": req.agent_id, **result}

    @router.post("/api/deliver/card")
    async def deliver_card(
        req: CardDeliveryRequest,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """Deliver a channel-neutral interaction card via the active adapter.

        The worker POSTs a neutral CardSpec body; the adapter renders the
        per-channel card (Slack Block Kit) and routes it to the agent's channel.
        """
        if settings.api_key and (not x_api_key or x_api_key != settings.api_key):
            raise HTTPException(401, "Invalid API key")

        spec = CardSpec(
            interaction_id=req.interaction_id,
            agent_id=req.agent_id,
            kind=req.kind,
            prompt=req.prompt,
            options=req.options,
            target=None,
            allow_hint=req.allow_hint,
        )
        send_result = await adapter.send_card(spec)
        result = send_result.to_response()
        await _log_dispatch(
            settings,
            agent_id=req.agent_id,
            content=req.prompt,
            send_result=result,
            kind="interaction_card",
        )
        return {"ok": result.get("ok", False), "agent_id": req.agent_id, **result}

    @router.post("/api/comms/delete")
    async def delete_dispatch(
        req: DeleteRequest,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        if settings.api_key and (not x_api_key or x_api_key != settings.api_key):
            raise HTTPException(401, "Invalid API key")
        try:
            ref = DeliveryRef.from_dict(req.delivery_ref)
            ok = await adapter.delete_message(ref=ref)
        except Exception as exc:
            logger.warning("delete_dispatch_error", error=str(exc)[:200])
            ok = False
        return {"ok": bool(ok)}

    app.include_router(router)
    return app


def _startup_error(settings: TelegramSettings) -> str | None:
    """Return an error string if the channel is not ready to boot, else None.

    Pure helper — no side effects — so it can be tested independently. Slack is
    the only channel.
    """
    if not settings.slack_bot_token or not settings.slack_app_token:
        return "slack_tokens_missing (need AEGIS_SLACK_BOT_TOKEN + AEGIS_SLACK_APP_TOKEN)"
    return None


async def run() -> None:
    """Start the Slack adapter (Socket Mode inbound) + delivery server."""
    from aegis_comms.telemetry import setup_telemetry

    setup_telemetry()

    settings = TelegramSettings()

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    err = _startup_error(settings)
    if err is not None:
        logger.error("startup_error", reason=err)
        return

    logger.info("slack_starting", core_url=settings.core_url)

    adapter = SlackAdapter(settings)
    app = create_delivery_app(adapter, settings)
    config = uvicorn.Config(app, host=settings.host, port=settings.port, log_level="info")
    server = uvicorn.Server(config)

    try:
        await asyncio.gather(
            adapter.start_listener(),
            server.serve(),
            _run_slack_socket_probe(adapter),
        )
    finally:
        await adapter.stop()
        logger.info("slack_stopped")


if __name__ == "__main__":
    asyncio.run(run())
