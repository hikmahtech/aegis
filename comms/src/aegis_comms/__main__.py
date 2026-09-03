"""AEGIS comms bot + delivery server (Slack).

Usage:
    python -m aegis_comms
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
import uvicorn
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from aegis_comms.adapters.base import CardSpec, DeliveryRef
from aegis_comms.adapters.slack import SlackAdapter
from aegis_comms.config import CommsSettings

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

# Largest raw body POST /api/ingest/voice will read. ElevenLabs Scribe caps at
# 1 GB but a phone voice note is kilobytes — this is the "don't let a bad
# client stream us out of memory" bound, not a product limit.
_MAX_VOICE_BYTES = 25 * 1024 * 1024


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
    settings: CommsSettings,
    *,
    agent_id: str,
    content: str,
    send_result: dict,
    kind: str,
) -> None:
    """Fire-and-forget POST to core /api/chat/dispatches so every outbound
    message lands as a role='dispatch' row in chat_history. This
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
    # {adapter,channel,ts} (the core 5a route stores it).
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
    no per-message chat/topic override, only the optional thread below.
    """

    text: str
    agent_id: str = "sebas"
    system_event: bool = False  # If true, send to General topic instead of agent topic
    # An existing thread ROOT — `{"channel": ..., "ts": ...}` — to reply under,
    # so a task's turns all land in one thread. None = post to the channel.
    thread_ref: dict | None = None


class DocumentAttachment(BaseModel):
    """A single document attachment."""

    filename: str
    content: str  # UTF-8 text; binary content should be base64 (not currently used)


class DocumentDeliveryRequest(BaseModel):
    """Document delivery request — sends one or more text documents to a topic."""

    documents: list[DocumentAttachment]
    caption: str = ""
    agent_id: str = "sebas"
    # Optional explicit destination ({"channel": ...}); None = agent's bound channel.
    target: dict | None = None


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

    The worker POSTs this neutral spec; the active adapter renders the card
    for its channel and routes it to the agent's channel.
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


def create_delivery_app(adapter: SlackAdapter, settings: CommsSettings) -> FastAPI:
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
            # True once Slack is fully configured (bot token + app token,
            # DB-or-env resolved). Lets a watchdog tell "intentionally idle,
            # never configured" apart from "should be connected but isn't"
            # (which the `inbound` block below already covers).
            "configured": bool(settings.slack_bot_token and settings.slack_app_token),
        }

        # The generic `inbound` block carries the Socket Mode liveness signal.
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

    @router.post("/api/deliver/message")
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

        # A malformed thread_ref degrades to a plain channel post rather than a
        # 500 — losing the threading is recoverable, losing the message is not.
        target = (
            {
                "channel": req.thread_ref.get("channel"),
                "thread_ts": req.thread_ref.get("ts"),
            }
            if req.thread_ref
            else None
        )
        send_result = await adapter.send_message(
            agent_id=req.agent_id, text=req.text, target=target
        )
        result = send_result.to_response()
        await _log_dispatch(
            settings,
            agent_id=req.agent_id,
            content=req.text,
            send_result=result,
            kind="deliver",
        )
        return {"ok": result.get("ok", False), "agent_id": req.agent_id, **result}

    @router.post("/api/deliver/document")
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
            target=req.target,
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

    @router.post("/api/ingest/voice")
    async def ingest_voice(
        request: Request,
        x_voice_secret: str | None = Header(default=None, alias="X-Voice-Secret"),
        filename: str = "voice.m4a",
    ) -> dict[str, Any]:
        """Voice-first capture from outside Slack (B3) — iOS Shortcut, etc.

        The recording is the RAW request body (not multipart: python-multipart
        is not a comms dependency, and "Get Contents of URL → Request Body:
        File" is the shape a Shortcut produces anyway). Transcribe, then hand
        the text to core's `kind="auto"` classifier, which decides between the
        Todoist Inbox and the knowledge store. Returns the transcript and the
        resolved lane so the Shortcut can show what happened.

        Auth is `voice_ingest_secret`, NOT the general comms api key — an
        unconfigured secret is a 503 (fail closed), never an open endpoint.
        """
        if not settings.voice_ingest_secret:
            raise HTTPException(503, "voice ingest not configured")
        if not x_voice_secret or not hmac.compare_digest(
            x_voice_secret, settings.voice_ingest_secret
        ):
            raise HTTPException(401, "Invalid voice ingest secret")
        if not settings.elevenlabs_api_key:
            raise HTTPException(503, "transcription not configured")

        audio = await request.body()
        if not audio:
            raise HTTPException(400, "empty audio body")
        if len(audio) > _MAX_VOICE_BYTES:
            raise HTTPException(413, "audio too large")

        from aegis_comms import elevenlabs
        from aegis_comms.slack_inbound import SlackCoreClient

        transcript = await elevenlabs.transcribe(
            audio,
            api_key=settings.elevenlabs_api_key,
            model_id=settings.elevenlabs_stt_model,
            filename=filename,
        )
        if not transcript:
            raise HTTPException(502, "transcription failed")

        ext_id = f"voice:{hashlib.sha256(transcript.encode()).hexdigest()[:16]}"
        # Not Slack-specific despite the name — it is just the core HTTP client.
        result = await SlackCoreClient(settings).capture(
            text=transcript, external_id=ext_id, kind="auto"
        )
        if not result:
            raise HTTPException(502, "core capture failed")
        logger.info(
            "voice_ingest_captured", lane=result.get("lane"), external_id=ext_id
        )
        return {
            "ok": True,
            "transcript": transcript,
            "lane": result.get("lane"),
            "task_ref": result.get("task_ref"),
            "content_id": result.get("content_id"),
        }

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


def _startup_error(settings: CommsSettings) -> str | None:
    """Return a reason string if Slack is not configured, else None.

    Pure helper — no side effects — so it can be tested independently. Slack is
    the only channel. Historically this gated whether `run()` booted at all;
    it no longer does (comms must stay up with or without Slack) — it is now
    used only to produce a human-readable reason for the `slack_disabled` log.
    """
    if not settings.slack_bot_token or not settings.slack_app_token:
        return "slack_tokens_missing (need AEGIS_SLACK_BOT_TOKEN + AEGIS_SLACK_APP_TOKEN, env or DB)"
    return None


async def _fetch_resolved_slack_config(settings: CommsSettings) -> dict[str, Any] | None:
    """GET the DB-resolved Slack config from core: `{configured, bot_token,
    app_token, channel}` (core itself already falls back to its own env vars,
    so this is cleartext-resolved either way).

    Comms has no DB access of its own — this is the only way it learns about
    tokens set via the admin UI. Returns None on ANY failure (core down, 404,
    network, bad body) so the caller falls back to comms' own env-sourced
    settings. Mirrors the existing agent-fetch httpx pattern in
    `adapters/slack.py`. Never raises.
    """
    if not settings.core_url:
        return None
    url = f"{settings.core_url.rstrip('/')}/api/internal/slack-config"
    headers = {"X-API-Key": settings.api_key} if settings.api_key else {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:  # noqa: BLE001 — fall back to env config on any error
        logger.warning("slack_config_fetch_failed", error=str(exc)[:200])
        return None


def _merge_slack_config(settings: CommsSettings, db_config: dict[str, Any] | None) -> None:
    """Merge the DB-resolved Slack config onto `settings` in place.

    DB value wins when present (non-empty); otherwise the env-sourced value
    pydantic-settings already loaded onto `settings` is kept untouched. A
    None `db_config` (fetch failed, or core has nothing) is a no-op — pure
    env fallback.
    """
    if not db_config:
        return
    settings.slack_bot_token = db_config.get("bot_token") or settings.slack_bot_token
    settings.slack_app_token = db_config.get("app_token") or settings.slack_app_token
    settings.channel = db_config.get("channel") or settings.channel
    # Self-capture knobs: the admin Integrations page writes them to core's DB,
    # so without this merge they'd be permanently blank in comms and the whole
    # feature would be a silent no-op.
    settings.slack_owner_member_id = (
        db_config.get("owner_member_id") or settings.slack_owner_member_id
    )
    settings.slack_saveit_emoji = (
        db_config.get("saveit_emoji") or settings.slack_saveit_emoji
    )
    settings.slack_note_to_self_channel = (
        db_config.get("note_to_self_channel") or settings.slack_note_to_self_channel
    )


async def run() -> None:
    """Start the delivery server (always) + Slack Socket Mode inbound (only
    when Slack ends up configured, via DB or env).

    comms must be resilient to running with or without Slack: `/api/health`
    and `/api/deliver/*` need to be reachable regardless, so core/worker and
    the watchdog never see comms as "down" just because nobody has wired up
    Slack yet. This function must never exit/return before the delivery
    server is up, and never `return` early because Slack isn't configured.
    """
    from aegis_comms.telemetry import setup_telemetry

    setup_telemetry()

    settings = CommsSettings()

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

    db_config = await _fetch_resolved_slack_config(settings)
    _merge_slack_config(settings, db_config)

    slack_ready = bool(settings.slack_bot_token and settings.slack_app_token)

    # Always build the delivery app + server — /api/health and /api/deliver/*
    # must be up whether or not Slack is configured.
    adapter = SlackAdapter(settings)
    app = create_delivery_app(adapter, settings)
    config = uvicorn.Config(app, host=settings.host, port=settings.port, log_level="info")
    server = uvicorn.Server(config)

    if slack_ready:
        logger.info("slack_starting", core_url=settings.core_url)
        try:
            await asyncio.gather(
                adapter.start_listener(),
                server.serve(),
                _run_slack_socket_probe(adapter),
            )
        finally:
            await adapter.stop()
            logger.info("slack_stopped")
    else:
        # Expected/idle state, not an error — log at info once and just serve
        # the delivery app (no Socket Mode listener, no liveness probe).
        logger.info("slack_disabled", reason=_startup_error(settings))
        await server.serve()


if __name__ == "__main__":
    asyncio.run(run())
