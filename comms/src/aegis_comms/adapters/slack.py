"""SlackAdapter — AEGIS's comms channel over `slack_sdk` AsyncWebClient.

OUTBOUND ONLY (Slice 4). Resolves an agent to its Slack channel + persona
(username/icon) via the core `/api/agents/{id}` lookup (cached), falling back to
resolving `#aegis-<short>` by name through `conversations_list`. Message bodies
are converted from light HTML to Slack mrkdwn (`html_to_mrkdwn`); interaction
cards render as Block Kit (`render_slack_blocks`) with a stable callback-button
identity so the resolve route is identical across channels.

Inbound (Socket Mode) lands in `start_listener`: it builds an `AsyncApp`, the
`slack_channel_id -> agent_id` map, and a `SlackInbound` (in `slack_inbound.py`)
whose pure `on_*` methods hold the routing + core-call logic; the bolt handlers
here are thin wrappers over them.
"""

from __future__ import annotations

import httpx
import structlog
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from aegis_comms.adapters.base import CardSpec, DeliveryRef, SendResult
from aegis_comms.cards import render_slack_blocks
from aegis_comms.format import html_to_mrkdwn

_logger = structlog.get_logger()

# Slack message body cap is 40k chars; we chunk well under that so a card's
# blocks/fallback stay comfortable and edits remain cheap.
_SLACK_MAX_CHARS = 2800

# Per-agent persona icon for chat:write.customize. Defaults to a robot.
_AGENT_ICON = {
    "sebas": ":bust_in_silhouette:",
    "raphael": ":books:",
    "maou": ":moneybag:",
    "pandoras-actor": ":robot_face:",
}
_DEFAULT_ICON = ":robot_face:"


def _split_message(text: str, limit: int = _SLACK_MAX_CHARS) -> list[str]:
    """Split on line boundaries where possible; hard-cut overlong lines."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(buf) + len(line) > limit:
            chunks.append(buf)
            buf = line
        else:
            buf += line
    if buf:
        chunks.append(buf)
    return chunks


def _short_agent(agent_id: str) -> str:
    """Channel-name stem: `pandoras-actor` -> `pandora`, else the id."""
    if agent_id == "pandoras-actor":
        return "pandora"
    return agent_id


async def handle_hint_open(client, body) -> None:
    """Open the hint modal for a Gate-0 card. Interaction stays pending."""
    from aegis_comms.slack_modal import build_hint_modal

    actions = body.get("actions") or []
    value = actions[0].get("value", "") if actions else ""
    parts = value.split(":", 2)  # interaction:{id}:hint_open
    interaction_id = parts[1] if len(parts) > 1 else ""
    if not interaction_id:
        return
    title = ((body.get("message") or {}).get("text")) or ""
    try:
        await client.views_open(
            trigger_id=body.get("trigger_id", ""),
            view=build_hint_modal(interaction_id, title),
        )
    except Exception as exc:  # noqa: BLE001 — original card stays usable
        _logger.warning("slack_views_open_failed", error=str(exc))


async def handle_hint_submit(core, body) -> None:
    """Resolve the pending interaction with the modal's free-text hint."""
    from aegis_comms.slack_modal import parse_view_submission

    parsed = parse_view_submission(body)
    if parsed is None:
        return
    interaction_id, text = parsed
    await core.resolve_interaction(interaction_id=interaction_id, value=f"hint:{text}")


class SlackAdapter:
    """AEGIS's comms channel backed by a Slack `AsyncWebClient`."""

    name = "slack"

    def __init__(self, settings) -> None:
        self._settings = settings
        self._client = AsyncWebClient(token=settings.slack_bot_token)
        self._core_url = settings.core_url.rstrip("/")
        self._api_key = settings.api_key
        self._http: httpx.AsyncClient | None = None
        # The live Socket Mode handler, set once start_listener() builds it.
        # Polled by the comms socket-liveness probe via is_connected().
        self._socket_handler = None
        # agent_id -> (channel_id, username, icon_emoji, voice_id); only POSITIVE
        # resolutions (non-None channel) are cached so a transient failure
        # (core fetch error, name lookup miss) does not poison the cache forever.
        self._cache: dict[str, tuple[str | None, str, str, str]] = {}

    def _httpx(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=10.0)
        return self._http

    async def _fetch_agent(self, agent_id: str) -> dict:
        headers = {"X-API-Key": self._api_key} if self._api_key else {}
        resp = await self._httpx().get(f"{self._core_url}/api/agents/{agent_id}", headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def _resolve_channel_by_name(self, name: str) -> str | None:
        """Find a channel id by name via conversations_list (handles pagination)."""
        cursor: str | None = None
        while True:
            resp = await self._client.conversations_list(
                limit=200,
                cursor=cursor,
                exclude_archived=True,
                types="public_channel,private_channel",
            )
            for ch in resp.get("channels", []):
                if ch.get("name") == name:
                    return ch.get("id")
            cursor = (resp.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                return None

    async def _resolve(self, agent_id: str) -> tuple[str | None, str, str, str]:
        """Resolve (channel_id, username, icon_emoji, voice_id) for an agent, cached."""
        if agent_id in self._cache:
            return self._cache[agent_id]

        icon = _AGENT_ICON.get(agent_id, _DEFAULT_ICON)
        username = agent_id
        channel: str | None = None
        voice_id = ""
        # Channel-name stem + icon are metadata-overridable; fall back to the
        # shipped constants (so a custom agent isn't stuck with the robot icon
        # or an id-based channel name).
        stem = _short_agent(agent_id)
        try:
            cfg = await self._fetch_agent(agent_id)
            username = cfg.get("name") or agent_id
            channel = cfg.get("slack_channel_id") or None
            voice_id = cfg.get("elevenlabs_voice_id") or ""
            meta = cfg.get("metadata") or {}
            icon = meta.get("slack_icon") or _AGENT_ICON.get(agent_id, _DEFAULT_ICON)
            aliases = meta.get("mention_aliases") or []
            if aliases:
                stem = str(aliases[0])
        except Exception as exc:  # noqa: BLE001 — best-effort; fall back to name lookup
            _logger.warning("slack_agent_lookup_failed", agent_id=agent_id, error=str(exc))

        if not channel:
            try:
                channel = await self._resolve_channel_by_name(f"aegis-{stem}")
            except SlackApiError as exc:
                _logger.warning(
                    "slack_channel_name_lookup_failed", agent_id=agent_id, error=str(exc)
                )

        result = (channel, username, icon, voice_id)
        # Only cache positive resolutions; a None channel means the core fetch
        # failed or no matching channel was found — leave the cache empty so the
        # next call can re-resolve once the failure is transient.
        if channel is not None:
            self._cache[agent_id] = result
        return result

    def _target_channel(self, channel: str | None, target: dict | None) -> str | None:
        if target and target.get("channel"):
            return target["channel"]
        return channel

    async def send_message(
        self,
        *,
        agent_id: str,
        text: str,
        target: dict | None = None,
        reply_markup: dict
        | None = None,  # accepted for seam uniformity; Slack uses Block Kit send_card instead
    ) -> SendResult:
        if reply_markup is not None:
            _logger.debug("slack_reply_markup_ignored", agent_id=agent_id)
        channel, username, icon, _voice_id = await self._resolve(agent_id)
        channel = self._target_channel(channel, target)
        body = html_to_mrkdwn(text)
        first_ref: DeliveryRef | None = None
        try:
            for chunk in _split_message(body):
                resp = await self._client.chat_postMessage(
                    channel=channel,
                    text=chunk,
                    mrkdwn=True,
                    username=username,
                    icon_emoji=icon,
                )
                if first_ref is None:
                    first_ref = DeliveryRef("slack", {"channel": resp["channel"], "ts": resp["ts"]})
        except SlackApiError as exc:
            return SendResult(ok=False, used_html=False, error=str(exc))
        return SendResult(ok=True, ref=first_ref, used_html=False)

    async def send_system_event(self, *, text: str) -> SendResult:
        channel, _username, _icon, _voice_id = await self._resolve("system")
        if not channel:
            try:
                channel = await self._resolve_channel_by_name("aegis-general")
            except SlackApiError as exc:
                _logger.warning("slack_general_lookup_failed", error=str(exc))
        body = html_to_mrkdwn(text)
        try:
            resp = await self._client.chat_postMessage(
                channel=channel,
                text=body,
                mrkdwn=True,
                username="AEGIS",
                icon_emoji=":gear:",
            )
        except SlackApiError as exc:
            return SendResult(ok=False, used_html=False, error=str(exc))
        return SendResult(
            ok=True,
            ref=DeliveryRef("slack", {"channel": resp["channel"], "ts": resp["ts"]}),
            used_html=False,
        )

    async def send_document(
        self,
        *,
        agent_id: str,
        documents: list[dict],
        caption: str,
        target: dict | None = None,
        reply_markup: dict
        | None = None,  # accepted for seam uniformity; Slack uses Block Kit send_card instead
    ) -> SendResult:
        if reply_markup is not None:
            _logger.debug("slack_reply_markup_ignored", agent_id=agent_id)
        channel, _username, _icon, _voice_id = await self._resolve(agent_id)
        channel = self._target_channel(channel, target)
        first_ref: DeliveryRef | None = None
        try:
            for i, doc in enumerate(documents):
                resp = await self._client.files_upload_v2(
                    channel=channel,
                    content=doc["content"],
                    filename=doc["filename"],
                    initial_comment=caption if i == 0 else None,
                )
                if first_ref is None:
                    files = resp.get("files") or []
                    ts = files[0].get("ts") if files else None
                    data = {"channel": channel}
                    if ts:
                        data["ts"] = ts
                    first_ref = DeliveryRef("slack", data)
        except SlackApiError as exc:
            return SendResult(ok=False, used_html=False, error=str(exc))
        return SendResult(ok=True, ref=first_ref, used_html=False)

    async def send_voice(
        self,
        *,
        agent_id: str,
        text: str,
        target: dict | None = None,
    ) -> SendResult:
        """Synthesize `text` in the agent's ElevenLabs voice and upload an mp3.

        Additive to the text reply — callers post the text separately. No-ops
        (ok=False) when the agent has no voice_id or ElevenLabs isn't configured;
        the caller treats that as "text-only" and moves on.
        """
        channel, _username, _icon, voice_id = await self._resolve(agent_id)
        channel = self._target_channel(channel, target)
        if not voice_id or not self._settings.elevenlabs_api_key:
            return SendResult(ok=False, used_html=False, error="tts_not_configured")

        from aegis_comms import elevenlabs

        mp3 = await elevenlabs.synthesize(
            text,
            api_key=self._settings.elevenlabs_api_key,
            voice_id=voice_id,
            model_id=self._settings.elevenlabs_tts_model,
        )
        if not mp3:
            return SendResult(ok=False, used_html=False, error="tts_synthesis_failed")

        try:
            resp = await self._client.files_upload_v2(
                channel=channel,
                content=mp3,
                filename=f"{_short_agent(agent_id)}.mp3",
            )
        except SlackApiError as exc:
            return SendResult(ok=False, used_html=False, error=str(exc))
        files = resp.get("files") or []
        ts = files[0].get("ts") if files else None
        data: dict[str, str] = {"channel": channel}
        if ts:
            data["ts"] = ts
        return SendResult(ok=True, ref=DeliveryRef("slack", data), used_html=False)

    async def send_card(self, spec: CardSpec) -> SendResult:
        channel, username, icon, _voice_id = await self._resolve(spec.agent_id)
        channel = self._target_channel(channel, spec.target)
        blocks = render_slack_blocks(spec)
        try:
            resp = await self._client.chat_postMessage(
                channel=channel,
                blocks=blocks,
                text=html_to_mrkdwn(spec.prompt or ""),
                username=username,
                icon_emoji=icon,
            )
        except SlackApiError as exc:
            return SendResult(ok=False, used_html=False, error=str(exc))
        return SendResult(
            ok=True,
            ref=DeliveryRef("slack", {"channel": resp["channel"], "ts": resp["ts"]}),
            used_html=False,
        )

    async def edit_card(self, *, ref: DeliveryRef, text: str) -> None:
        await self._client.chat_update(
            channel=ref.data["channel"],
            ts=ref.data["ts"],
            text=html_to_mrkdwn(text),
            blocks=[],
        )

    async def delete_message(self, *, ref: DeliveryRef) -> bool:
        try:
            await self._client.chat_delete(channel=ref.data["channel"], ts=ref.data["ts"])
            return True
        except SlackApiError as exc:
            if (exc.response or {}).get("error") == "message_not_found":
                return True
            _logger.warning("slack_delete_failed", error=str(exc))
            return False

    async def _build_channel_agent_map(self) -> dict[str, str]:
        """Build `slack_channel_id -> agent_id` from /api/agents (reverse of
        `_resolve`). The map drives inbound routing in `SlackInbound`.
        """
        headers = {"X-API-Key": self._api_key} if self._api_key else {}
        try:
            resp = await self._httpx().get(f"{self._core_url}/api/agents", headers=headers)
            resp.raise_for_status()
            agents = resp.json()
        except Exception as exc:  # noqa: BLE001 — empty map is a safe degrade
            _logger.warning("slack_channel_map_fetch_failed", error=str(exc))
            return {}
        out: dict[str, str] = {}
        for agent in agents or []:
            channel_id = agent.get("slack_channel_id")
            agent_id = agent.get("id")
            if channel_id and agent_id:
                out[channel_id] = agent_id
        return out

    async def start_listener(self) -> None:
        """Run the Socket Mode inbound listener.

        Builds an `AsyncApp`, the `slack_channel_id -> agent_id` map, and a
        `SlackInbound`, registers thin bolt handlers that adapt the bolt kwargs
        onto `SlackInbound.on_*`, then blocks on the Socket Mode handler.
        """
        import re

        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        from slack_bolt.app.async_app import AsyncApp

        from aegis_comms.slack_inbound import SlackCoreClient, SlackInbound

        app = AsyncApp(token=self._settings.slack_bot_token)

        # The bot's own user id — used to ignore self-messages and to detect
        # app_mention routing. Best-effort; an unset id just disables those.
        bot_user_id: str | None = None
        try:
            auth = await self._client.auth_test()
            bot_user_id = auth.get("user_id")
        except SlackApiError as exc:
            _logger.warning("slack_auth_test_failed", error=str(exc))

        channel_agent_map = await self._build_channel_agent_map()
        core = SlackCoreClient(self._settings)
        inbound = SlackInbound(
            adapter=self,
            core=core,
            channel_agent_map=channel_agent_map,
            bot_user_id=bot_user_id,
            bot_token=self._settings.slack_bot_token,
            elevenlabs_api_key=self._settings.elevenlabs_api_key,
            elevenlabs_stt_model=self._settings.elevenlabs_stt_model,
        )

        @app.event("message")
        async def _on_message(event, logger):  # noqa: ANN001
            # Skip subtypes (edits/joins/bot_message) — we only route plain
            # user text; bot messages also carry a bot_id which on_message drops.
            if event.get("subtype"):
                return
            # Slack also fires `app_mention` for a message that @mentions the
            # bot; let that handler own it so we don't double-route.
            if bot_user_id and f"<@{bot_user_id}" in (event.get("text") or ""):
                return
            await inbound.on_message(
                channel_id=event.get("channel", ""),
                text=event.get("text", ""),
                user_id=event.get("user"),
                bot_id=event.get("bot_id"),
            )

        @app.event("app_mention")
        async def _on_app_mention(event):  # noqa: ANN001
            await inbound.on_message(
                channel_id=event.get("channel", ""),
                text=event.get("text", ""),
                user_id=event.get("user"),
                bot_id=event.get("bot_id"),
            )

        @app.action(re.compile(r"^interaction_"))
        async def _on_interaction(ack, action, body):  # noqa: ANN001
            await ack()
            channel = (body.get("channel") or {}).get("id", "")
            message_ts = (body.get("message") or {}).get("ts", "")
            await inbound.on_action(
                value=action.get("value", ""),
                channel_id=channel,
                message_ts=message_ts,
            )

        @app.action("hint_open")
        async def _on_hint_open(ack, body):  # noqa: ANN001
            await ack()
            await handle_hint_open(self._client, body)

        @app.view("hint_submit")
        async def _on_hint_submit(ack, body):  # noqa: ANN001
            await ack()
            await handle_hint_submit(core, body)

        @app.action("open_url")
        async def _on_open_url(ack):  # noqa: ANN001 — URL buttons need only an ack
            await ack()

        @app.command("/capture")
        async def _on_capture(ack, command, respond):  # noqa: ANN001
            await ack()
            reply = await inbound.on_capture(
                text=command.get("text", ""), user_id=command.get("user_id", "")
            )
            await respond(reply)

        @app.command("/status")
        async def _on_status(ack, respond):  # noqa: ANN001
            await ack()
            await respond(await inbound.on_status())

        @app.event("file_shared")
        async def _on_file(event, client):  # noqa: ANN001
            await inbound.on_file(
                file_id=event.get("file_id", ""),
                channel_id=event.get("channel_id") or event.get("channel", ""),
                caption="",
                client=client,
            )

        handler = AsyncSocketModeHandler(app, self._settings.slack_app_token)
        self._socket_handler = handler
        _logger.info("slack_socket_mode_connecting", bot_user_id=bot_user_id)
        await handler.start_async()

    async def is_connected(self) -> bool | None:
        """Socket Mode inbound liveness for the comms health probe.

        Returns True/False once the listener has built its handler (False covers
        a dropped or ping/pong-failing websocket), or None before start_listener
        has run — the probe treats None as "not started yet" and holds the
        last-good watermark rather than flapping to down during startup.
        """
        handler = self._socket_handler
        if handler is None:
            return None
        return await handler.client.is_connected()

    async def stop(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
