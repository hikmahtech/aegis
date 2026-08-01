"""Slack inbound — pure routing + a testable core client + on_* handlers.

Socket Mode inbound for the Slack channel. The decision logic lives in pure
functions (`route_message`, `parse_action`) and the `SlackInbound.on_*` methods
so tests exercise the logic directly without a live socket. The bolt handlers
(registered in `SlackAdapter.start_listener`) are thin wrappers that extract
fields from the bolt kwargs and call the matching `on_*` method.

Routing:
  (a) explicit `@<agent>` mention anywhere → async (AgentChatReplyFlow),
      mention stripped before the LLM sees it;
  (b) the bot itself @app_mention'd → async to the channel's agent;
  (c) the channel maps to pandora → async (kimi tools run minutes);
  (d) otherwise → sync `/api/chat` with the channel's agent (default sebas).

Core-call contracts match the bot's: POST /api/chat (sync),
POST /api/chat/agent-reply/trigger (async), POST /api/admin/capture (/capture),
POST /api/interactions/{id}/resolve (button), GET /api/health + /api/agents
(/status), POST /api/knowledge/ingest + PATCH the assistant row's delivery-ref.
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any

import httpx
import structlog

from aegis_comms.adapters.base import DeliveryRef

logger = structlog.get_logger()

# Default agent when a channel has no mapping (mirrors bot.py's "sebas").
_DEFAULT_AGENT = "sebas"

# Slack voice clips / audio uploads. mimetype starts with "audio/" or the name
# carries one of these extensions (Slack voice messages are typically mp4/m4a).
_AUDIO_EXTENSIONS = (".mp4", ".m4a", ".webm", ".mp3", ".ogg", ".wav", ".aac", ".flac")


# Reaction names that file your own message as a life fact when nothing is
# configured. Slack sends reaction names without colons ("brain", not ":brain:").
_DEFAULT_SAVEIT_EMOJI = "brain"


def _parse_emoji_set(raw: str) -> frozenset[str]:
    """Comma-separated reaction names → a normalized set (colons stripped)."""
    names = {n.strip().strip(":").lower() for n in (raw or "").split(",")}
    return frozenset(names - {""}) or frozenset({_DEFAULT_SAVEIT_EMOJI})


def _is_audio_file(name: str, mimetype: str) -> bool:
    """True if a shared Slack file looks like audio (voice note or upload)."""
    if mimetype.startswith("audio/"):
        return True
    return name.lower().endswith(_AUDIO_EXTENSIONS)


# Front-door conversation stickiness window. An ambiguous follow-up within this
# many seconds stays with the conversation's last agent.
# ponytail: fixed 30-min TTL; per-user tuning only if it ever matters.
_STICKY_TTL_SECONDS = 1800.0

# How long the derived (mention_map, async_agents) routing config is cached
# before re-fetching GET /api/agents. Short so admin Behavior-tab edits apply
# within a minute without a comms restart.
_ROUTING_CFG_TTL_SECONDS = 60.0

# Slack-label → downstream agent-id. Fallback only — the live map is derived
# per-request from each active agent's `metadata.mention_aliases` (default
# `[agent.id]`) via `_derive_mention_map`. This hardcoded copy is used when the
# `GET /api/agents` fetch fails, so an inbound message never crashes. The label
# `pandora` maps to agent id `pandoras-actor` (its seed mention_aliases).
_AGENT_MENTION_MAP = {
    "pandora": "pandoras-actor",
    "pandoras-actor": "pandoras-actor",
    "sebas": "sebas",
    "raphael": "raphael",
    "maou": "maou",
}

# Fallback set of agent ids dispatched async (kimi tools run minutes). Live set
# is derived from `metadata.async_dispatch` via `_derive_async_agents`.
_ASYNC_AGENTS = frozenset({"pandoras-actor"})


def _build_mention_re(mention_map: dict[str, str]) -> re.Pattern[str]:
    """Compile the `@<alias>` matcher for a given label→agent map."""
    return re.compile(
        r"(?<![\w])@("
        + "|".join(re.escape(n) for n in sorted(mention_map, key=len, reverse=True))
        + r")\b[:,]?",
        re.IGNORECASE,
    )


_AGENT_MENTION_RE = _build_mention_re(_AGENT_MENTION_MAP)


def _derive_mention_map(agents: list[dict] | None) -> dict[str, str]:
    """Build the Slack-label → agent-id map from active agents' metadata.

    Each agent contributes its `metadata.mention_aliases` (default `[agent.id]`)
    plus its own id, all lowercased. Returns {} when there's no usable data so
    the caller can fall back to `_AGENT_MENTION_MAP`.
    """
    out: dict[str, str] = {}
    for a in agents or []:
        aid = a.get("id")
        if not aid:
            continue
        aliases = (a.get("metadata") or {}).get("mention_aliases") or [aid]
        for alias in aliases:
            if alias:
                out[str(alias).lower()] = aid
        out.setdefault(str(aid).lower(), aid)  # id itself is always addressable
    return out


def _derive_async_agents(agents: list[dict] | None) -> set[str]:
    """Agent ids whose `metadata.async_dispatch` is truthy (slow/SSH agents)."""
    return {
        a["id"]
        for a in (agents or [])
        if a.get("id") and (a.get("metadata") or {}).get("async_dispatch")
    }


def _parse_agent_mention(
    text: str, mention_map: dict[str, str] | None = None
) -> tuple[str | None, str]:
    """Detect an `@<agent>` token anywhere in `text` (ported from bot.py).

    Returns `(target_agent, stripped_text)` when a known agent is mentioned;
    the mention is removed and surrounding whitespace collapsed so the LLM
    doesn't see a self-reference. First-found wins; `info@mail.com` does not
    false-positive (word-boundary match). `mention_map` defaults to the shipped
    `_AGENT_MENTION_MAP`; callers pass the DB-derived map to reach custom agents.
    """
    if not text:
        return None, text
    if mention_map is None:
        mention_map, mention_re = _AGENT_MENTION_MAP, _AGENT_MENTION_RE
    else:
        mention_re = _build_mention_re(mention_map)
    match = mention_re.search(text)
    if match is None:
        return None, text
    name = match.group(1).lower()
    target = mention_map.get(name)
    if target is None:
        return None, text
    cleaned = (text[: match.start()] + " " + text[match.end() :]).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return target, cleaned or "(no message body — please describe what you want)"


def _strip_bot_mention(text: str, bot_user_id: str | None) -> str:
    """Remove a Slack `<@U…>` mention of the bot and collapse whitespace."""
    if not bot_user_id:
        return text
    cleaned = re.sub(rf"<@{re.escape(bot_user_id)}(\|[^>]*)?>", " ", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def route_message(
    channel_id: str,
    text: str,
    channel_agent_map: dict[str, str],
    mention_bot_id: str | None = None,
    mention_map: dict[str, str] | None = None,
    async_agents: set[str] | frozenset[str] | None = None,
) -> tuple[str, str, str]:
    """Decide how to route an inbound Slack message (pure; mirrors bot._message).

    Returns `(mode, agent_id, clean_text)` where mode is "sync" | "async" | "route":
      - explicit `@<agent>` mention anywhere → ("async", that agent, stripped);
      - UNBOUND channel (no agent mapping) + no `@agent` → ("route", "", clean):
        the caller resolves the agent via the front-door intent classifier
        (bot @mention token stripped so the classifier sees clean text);
      - bound channel + bot @app_mention'd → ("async", channel's agent, stripped);
      - bound channel maps to an async-dispatch agent → ("async", agent, text);
      - bound channel otherwise → ("sync", channel's agent, text).

    `mention_map` (Slack-label → agent-id) and `async_agents` (ids that dispatch
    async) default to the shipped constants; callers pass the DB-derived values
    so custom/renamed agents route correctly.
    """
    if async_agents is None:
        async_agents = _ASYNC_AGENTS
    mentioned_agent, stripped = _parse_agent_mention(text, mention_map)
    if mentioned_agent is not None:
        return "async", mentioned_agent, stripped

    channel_agent = channel_agent_map.get(channel_id)
    if channel_agent is None:
        # Front door: unbound channel, no explicit @agent → intent-route.
        clean = (
            _strip_bot_mention(text, mention_bot_id)
            if mention_bot_id and f"<@{mention_bot_id}" in text
            else text
        )
        return "route", "", clean

    if mention_bot_id and f"<@{mention_bot_id}" in text:
        return "async", channel_agent, _strip_bot_mention(text, mention_bot_id)

    if channel_agent in async_agents:
        return "async", channel_agent, text

    return "sync", channel_agent, text


def parse_action(value: str) -> tuple[str, str]:
    """Split `interaction:{id}:{value}` into `(interaction_id, value)`.

    Uses `split(":", 2)` so a value containing colons (e.g. `option:a`) is
    preserved. Mirrors bot.py::handle_interaction_callback.
    """
    parts = value.split(":", 2)
    # parts[0] is the "interaction" literal.
    interaction_id = parts[1] if len(parts) > 1 else ""
    val = parts[2] if len(parts) > 2 else ""
    return interaction_id, val


class SlackCoreClient:
    """Async httpx client for the Core API calls the Slack inbound makes.

    Auth: HTTP basic (admin user/pass) plus the `X-API-Key` header when an
    api key is configured.
    """

    def __init__(self, settings) -> None:
        self._core_url = settings.core_url.rstrip("/")
        self._api_key = settings.api_key
        self._auth = (settings.admin_username, settings.admin_password)

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self._api_key} if self._api_key else {}

    async def _post(
        self, path: str, data: dict, timeout: float = 90, error_sink: dict | None = None
    ) -> Any:
        """POST to Core. Returns the JSON body, or None on any failure.

        Pass `error_sink` to also capture WHY it failed (`reason` key) — Core
        puts the real cause (LLM auth error, tool crash, …) in the 500 body,
        and callers that report to a human should say that rather than a
        generic "couldn't reach Core".
        """
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{self._core_url}{path}",
                    json=data,
                    auth=self._auth,
                    headers=self._headers(),
                )
                if resp.status_code in (200, 202):
                    return resp.json()
                logger.warning(
                    "slack_core_post_non_2xx",
                    path=path,
                    status=resp.status_code,
                    body=resp.text[:200],
                )
                if error_sink is not None:
                    error_sink["reason"] = f"Core API returned {resp.status_code}: {resp.text[:400]}"
        except Exception as exc:  # noqa: BLE001 — best-effort; caller degrades
            logger.warning(
                "slack_core_post_failed",
                path=path,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            if error_sink is not None:
                error_sink["reason"] = f"Could not reach Core API: {type(exc).__name__}: {exc}"
        return None

    async def _patch(self, path: str, data: dict, timeout: float = 30) -> Any:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.patch(
                    f"{self._core_url}{path}",
                    json=data,
                    auth=self._auth,
                    headers=self._headers(),
                )
                if resp.status_code == 200:
                    return resp.json()
                logger.warning(
                    "slack_core_patch_non_200",
                    path=path,
                    status=resp.status_code,
                    body=resp.text[:200],
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("slack_core_patch_failed", path=path, error=str(exc))
        return None

    async def _get(self, path: str, timeout: float = 15) -> Any:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    f"{self._core_url}{path}",
                    auth=self._auth,
                    headers=self._headers(),
                )
                if resp.status_code == 200:
                    return resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("slack_core_get_failed", path=path, error=str(exc))
        return None

    async def chat(
        self, *, agent_id: str, message: str, thread_id: str, delivery_ref: dict | None
    ) -> dict:
        """POST /api/chat (sync) with the neutral delivery_ref block.

        Returns the result dict ({response, assistant_message_id, …}). On
        failure returns a dict carrying a user-facing `response` so the caller
        always has something to post.
        """
        payload: dict[str, Any] = {
            "agent_id": agent_id,
            "message": message,
            "thread_id": thread_id,
            "delivery_ref": delivery_ref,
        }
        error_sink: dict = {}
        result = await self._post("/api/chat", payload, timeout=600, error_sink=error_sink)
        if result is None:
            reason = error_sink.get("reason") or "Core API call failed."
            return {"response": f":warning: {reason}", "assistant_message_id": None}
        return result

    async def route_intent(self, *, message: str) -> dict:
        """POST /api/chat/route → {agent_id, method}. Safe-degrades to sebas/default."""
        result = await self._post("/api/chat/route", {"message": message}, timeout=30)
        if isinstance(result, dict) and result.get("agent_id"):
            return {"agent_id": result["agent_id"], "method": result.get("method", "llm")}
        return {"agent_id": _DEFAULT_AGENT, "method": "default"}

    async def agent_reply_trigger(
        self, *, target_agent: str, message: str, thread_id: str, reply_chat_id: int
    ) -> dict | None:
        """POST /api/chat/agent-reply/trigger — spawn AgentChatReplyFlow (async)."""
        payload = {
            "target_agent": target_agent,
            "message": message,
            "thread_id": thread_id,
            "reply_chat_id": reply_chat_id,
        }
        return await self._post("/api/chat/agent-reply/trigger", payload, timeout=15)

    async def capture(
        self, *, text: str, external_id: str, kind: str = "task"
    ) -> dict | None:
        """POST /api/admin/capture — Todoist Inbox task, or a `life_fact`.

        `kind="life_fact"` files the text in the knowledge store instead of
        Todoist; the response carries `content_id` rather than `task_ref`.
        """
        payload = {
            "text": text[:2000],
            "source": "slack",
            "external_id": external_id,
            "kind": kind,
        }
        return await self._post("/api/admin/capture", payload, timeout=30)

    async def resolve_interaction(
        self, *, interaction_id: str, value: str, note: str = ""
    ) -> dict | None:
        """POST /api/interactions/{id}/resolve — record a button choice.

        `note` — optional human reason typed into the card's note input; the
        core learning loop (record_correction_from_interaction) turns it into
        a durable agent_memory lesson."""
        response: dict = {"value": value}
        if note:
            response["note"] = note[:500]
        return await self._post(
            f"/api/interactions/{interaction_id}/resolve",
            {"response": response},
            timeout=30,
        )

    async def attach_delivery_ref(self, *, message_id: str, delivery_ref: dict) -> dict | None:
        """POST /api/chat/messages/{id}/delivery-ref — attach the reply ref.

        The core route is POST; using PATCH returns 405 and silently drops
        the ref.
        """
        return await self._post(
            f"/api/chat/messages/{message_id}/delivery-ref",
            {"delivery_ref": delivery_ref},
            timeout=30,
        )

    async def health(self) -> dict | None:
        """GET /api/health."""
        return await self._get("/api/health")

    async def agents(self) -> list | None:
        """GET /api/agents."""
        return await self._get("/api/agents")

    async def status_digest(self, *, hours: int = 24) -> dict | None:
        """GET /api/observability/status-digest — the shared aggregate behind
        `/status` and the `system_status` chat tool (comms has no aegis-core
        dependency, so it reaches the data over HTTP)."""
        return await self._get(f"/api/observability/status-digest?hours={hours}")

    async def knowledge_ingest(
        self,
        *,
        url: str,
        title: str,
        raw_text: str,
        tags: list[str] | None = None,
        source_type: str = "document",
    ) -> str | None:
        """POST /api/knowledge/ingest — returns content_id on success."""
        result = await self._post(
            "/api/knowledge/ingest",
            {
                "url": url,
                "title": title,
                "source_type": source_type,
                "raw_text": raw_text[:100_000],
                "tags": tags or [],
            },
        )
        if result and isinstance(result, dict):
            return result.get("content_id")
        return None


class SlackInbound:
    """Testable Slack inbound: routing + core calls + adapter posting.

    Holds the outbound `SlackAdapter` (reused for posting replies/acks + card
    edits), a `SlackCoreClient`, and the `channel_id -> agent_id` map (the
    reverse of the adapter's resolve). The `on_*` methods are what the thin
    bolt handlers call; they take no bolt objects so tests drive them directly.
    """

    def __init__(
        self,
        *,
        adapter,
        core: SlackCoreClient,
        channel_agent_map: dict[str, str],
        bot_user_id: str | None = None,
        bot_token: str = "",
        elevenlabs_api_key: str = "",
        elevenlabs_stt_model: str = "scribe_v1",
        owner_member_id: str = "",
        saveit_emoji: str = "",
        note_to_self_channel: str = "",
    ) -> None:
        self._adapter = adapter
        self._core = core
        self._channel_agent_map = channel_agent_map
        self._bot_user_id = bot_user_id
        self._bot_token = bot_token
        self._elevenlabs_api_key = elevenlabs_api_key
        self._elevenlabs_stt_model = elevenlabs_stt_model
        # Curated self-signal ingest (B2). Blank owner id disables both lanes.
        self._owner_member_id = (owner_member_id or "").strip()
        self._saveit_emojis = _parse_emoji_set(saveit_emoji)
        self._note_to_self_channel = (note_to_self_channel or "").strip()
        # channel_id -> (agent_id, monotonic_ts); ephemeral conversation context.
        self._sticky: dict[str, tuple[str, float]] = {}
        # Cached (mention_map, async_agents) derived from GET /api/agents.
        self._routing_cfg: tuple[dict[str, str], set[str]] | None = None
        self._routing_cfg_ts: float = 0.0

    async def _routing_config(self) -> tuple[dict[str, str], set[str]]:
        """(_mention_map, async_agents) derived from active agents' metadata,
        cached for `_ROUTING_CFG_TTL_SECONDS`. Degrades to the shipped constants
        if `GET /api/agents` fails, so routing never crashes."""
        now = time.monotonic()
        if self._routing_cfg is not None and now - self._routing_cfg_ts < _ROUTING_CFG_TTL_SECONDS:
            return self._routing_cfg
        try:
            agents = await self._core.agents()
        except Exception as exc:  # noqa: BLE001 — routing must never break inbound
            logger.warning("slack_routing_config_fetch_failed", error=str(exc)[:200])
            agents = None
        if isinstance(agents, list) and agents:
            mention_map = _derive_mention_map(agents) or dict(_AGENT_MENTION_MAP)
            async_agents = _derive_async_agents(agents)
        else:
            mention_map, async_agents = dict(_AGENT_MENTION_MAP), set(_ASYNC_AGENTS)
        self._routing_cfg = (mention_map, async_agents)
        self._routing_cfg_ts = now
        return self._routing_cfg

    def _sticky_get(self, channel_id: str, now: float) -> str | None:
        """Return the channel's sticky agent if set and within the TTL, else None."""
        entry = self._sticky.get(channel_id)
        if entry is None:
            return None
        agent, ts = entry
        if now - ts > _STICKY_TTL_SECONDS:
            self._sticky.pop(channel_id, None)
            return None
        return agent

    def _sticky_set(self, channel_id: str, agent_id: str, now: float) -> None:
        """Remember the agent that handled this channel's latest turn."""
        self._sticky[channel_id] = (agent_id, now)

    async def _sync_chat(
        self, *, agent_id: str, clean_text: str, thread_id: str, channel_id: str
    ) -> None:
        """Sync chat: POST /api/chat → post reply → attach delivery-ref.

        Shared by the sync branch and the async-trigger-failed fallback
        (mirrors bot.py::_send_chat + the two-step delivery-ref attach).
        """
        result = await self._core.chat(
            agent_id=agent_id,
            message=clean_text,
            thread_id=thread_id,
            delivery_ref={"adapter": "slack", "channel": channel_id},
        )
        reply_text = result.get("response", "No response from agent.")
        assistant_message_id = result.get("assistant_message_id")

        send_result = await self._adapter.send_message(
            agent_id=agent_id,
            text=reply_text,
            target={"channel": channel_id},
        )

        if assistant_message_id and send_result.ok and send_result.ref is not None:
            await self._core.attach_delivery_ref(
                message_id=assistant_message_id,
                delivery_ref=send_result.ref.to_dict(),
            )

    async def on_message(
        self,
        *,
        channel_id: str,
        text: str,
        user_id: str | None,
        bot_id: str | None = None,
        ts: str = "",
    ) -> None:
        """Route a text message (sync chat vs async agent-reply).

        Ignores the bot's own messages (a `bot_id` is present, or `user_id`
        equals our bot user id) to avoid self-reply loops.

        Note-to-self short-circuit: in the configured note-to-self channel, the
        OWNER's own messages are filed as life facts instead of being routed to
        an agent. Anyone else posting there still gets normal chat routing.

        Async path (mirrors bot.py::_dispatch_agent_reply): ack ONLY if the
        trigger returned 2xx; on any failure fall back to the sync path so the
        user still gets a response instead of silence.
        """
        if bot_id:
            return
        if self._bot_user_id and user_id == self._bot_user_id:
            return
        if not text:
            return

        if self._is_note_to_self(channel_id=channel_id, user_id=user_id):
            await self._ingest_self_signal(channel_id=channel_id, ts=ts, text=text)
            return

        await self._route_and_dispatch(channel_id=channel_id, text=text)

    # --- curated self-signal ingest (B2) ------------------------------------

    def _is_note_to_self(self, *, channel_id: str, user_id: str | None) -> bool:
        """True only for the OWNER posting in the configured note-to-self channel.

        Fails safe: no owner id configured (or no channel configured) ⇒ False,
        so an unconfigured deployment ingests nothing.
        """
        if not self._owner_member_id or not self._note_to_self_channel:
            return False
        if channel_id != self._note_to_self_channel:
            return False
        return user_id == self._owner_member_id

    async def on_reaction(
        self,
        *,
        reaction: str,
        user_id: str,
        item_user: str,
        channel_id: str,
        ts: str,
        client: Any,
    ) -> None:
        """`reaction_added` — file MY OWN message as a life fact.

        Two hard, independent security filters, both before any Slack API call
        so another person's message body is never even fetched:

          1. `user_id` (who reacted) must be the configured owner;
          2. `item_user` (who wrote the message) must be that same owner.

        Unset `slack_owner_member_id` ⇒ both fail ⇒ nothing is ingested. Any
        other emoji, or a non-message item (no channel/ts), is ignored.
        """
        if not self._owner_member_id:
            return
        if user_id != self._owner_member_id:
            return
        if item_user != self._owner_member_id:
            return
        if reaction.strip().strip(":").lower() not in self._saveit_emojis:
            return
        if not channel_id or not ts:
            return

        text = await self._fetch_message_text(client, channel_id, ts)
        # A blank/failed fetch is dropped by _ingest_self_signal's own guard.
        await self._ingest_self_signal(channel_id=channel_id, ts=ts, text=text)

    @staticmethod
    async def _fetch_message_text(client: Any, channel_id: str, ts: str) -> str:
        """The reacted-to message's text via conversations.history, or "".

        Only ever called after both owner filters have passed.
        """
        try:
            resp = await client.conversations_history(
                channel=channel_id, latest=ts, oldest=ts, inclusive=True, limit=1
            )
            messages = (resp or {}).get("messages") or []
        except Exception as exc:  # noqa: BLE001 — a fetch failure must not crash inbound
            logger.warning("slack_reaction_history_failed", error=str(exc)[:200])
            return ""
        if not messages:
            logger.warning("slack_reaction_message_not_found", channel=channel_id, ts=ts)
            return ""
        return (messages[0].get("text") or "").strip()

    async def _ingest_self_signal(self, *, channel_id: str, ts: str, text: str) -> None:
        """File an owner self-signal into the knowledge store as a `life_fact`.

        `slack://{channel}/{ts}` is the dedupe key — core derives content_id
        from the url and upserts, so a duplicate `reaction_added` for the same
        message refreshes one row instead of creating a second.
        """
        text = (text or "").strip()
        if not text:
            return
        content_id = await self._core.knowledge_ingest(
            url=f"slack://{channel_id}/{ts}",
            title=text[:200],
            raw_text=text,
            tags=["life_fact", "slack"],
            source_type="life_fact",
        )
        if content_id:
            logger.info("slack_self_signal_ingested", channel=channel_id, ts=ts)
        else:
            logger.warning("slack_self_signal_ingest_failed", channel=channel_id, ts=ts)

    async def _route_and_dispatch(self, *, channel_id: str, text: str) -> None:
        """Route an inbound message body and dispatch it (sync chat vs async).

        Shared post-routing core for both typed messages (`on_message`) and
        transcribed voice notes (`on_file` audio branch) so a voice note behaves
        exactly like a typed message: @mention parsing, sticky-agent, and the
        sync/async split all apply identically.
        """
        mention_map, async_agents = await self._routing_config()
        mode, agent_id, clean_text = route_message(
            channel_id,
            text,
            self._channel_agent_map,
            self._bot_user_id,
            mention_map=mention_map,
            async_agents=async_agents,
        )
        now = time.monotonic()

        if mode == "route":
            routed = await self._core.route_intent(message=clean_text)
            routed_agent = routed.get("agent_id", _DEFAULT_AGENT)
            method = routed.get("method", "default")
            sticky = self._sticky_get(channel_id, now)
            # Clear keyword → route by content. Ambiguous (llm/default) + a fresh
            # sticky agent → stay with the conversation's agent.
            agent_id = sticky if (method != "keyword" and sticky is not None) else routed_agent
            mode = "async" if agent_id in async_agents else "sync"

        # Remember the resolved agent as this channel's conversation context so the
        # next ambiguous follow-up sticks (including after an explicit @mention).
        self._sticky_set(channel_id, agent_id, now)

        thread_id = f"slack-{channel_id}-{agent_id}"

        if mode == "async":
            triggered = await self._core.agent_reply_trigger(
                target_agent=agent_id,
                message=clean_text,
                thread_id=thread_id,
                reply_chat_id=0,
            )
            if triggered is not None:
                # Short ack so the user knows it's queued; pandora's kimi
                # tools can legitimately run minutes.
                await self._adapter.send_message(
                    agent_id=agent_id,
                    text=f"🤖 Routing to @{agent_id}…",
                    target={"channel": channel_id},
                )
                return
            # Trigger failed (non-2xx or transport error) — fall back to sync
            # so the user still gets a reply rather than silence.
            logger.warning(
                "slack_async_trigger_failed_sync_fallback",
                agent_id=agent_id,
                channel_id=channel_id,
            )

        # Sync path: chat, post the reply, then attach the delivery-ref.
        await self._sync_chat(
            agent_id=agent_id,
            clean_text=clean_text,
            thread_id=thread_id,
            channel_id=channel_id,
        )

    async def on_action(
        self, *, value: str, channel_id: str, message_ts: str, note: str = ""
    ) -> None:
        """Resolve an interaction button, then stamp the card ONLY on success.

        `value` is the button payload `interaction:{id}:{v}`. `note` is the
        optional free-text from the card's note input — passed through so a
        correction becomes a durable agent lesson. Retries up to 3× on
        transport failure (mirrors bot.py::handle_interaction_callback). The
        card is edited (buttons cleared) ONLY when the result status is
        `resolved` or `already_resolved`; on failure the buttons are left intact
        so the user can re-click.
        """
        interaction_id, val = parse_action(value)
        if not interaction_id:
            return

        result = None
        for attempt in range(1, 4):
            result = await self._core.resolve_interaction(
                interaction_id=interaction_id, value=val, note=note
            )
            if result is not None:
                break
            if attempt < 3:
                logger.warning(
                    "slack_action_resolve_retrying",
                    interaction_id=interaction_id,
                    attempt=attempt,
                )

        status = (result or {}).get("status", "")
        if status in ("resolved", "already_resolved"):
            ref = DeliveryRef("slack", {"channel": channel_id, "ts": message_ts})
            await self._adapter.edit_card(ref=ref, text=f"✅ {val}")
        else:
            logger.warning(
                "slack_action_resolve_failed",
                interaction_id=interaction_id,
                result=result,
            )

    async def on_capture(self, *, text: str, user_id: str) -> str:
        """`/capture <text>` — drop a task into the Todoist Inbox.

        Idempotency key `slack:{user_id}:{sha256(text)[:16]}` so re-sending the
        same text from the same user is a no-op. Returns a user-facing string.
        """
        text = (text or "").strip()
        if not text:
            return "Usage: `/capture buy milk`"
        ext_id = f"slack:{user_id}:{hashlib.sha256(text.encode()).hexdigest()[:16]}"
        result = await self._core.capture(text=text, external_id=ext_id)
        if result and result.get("task_ref"):
            return f"📥 Captured to Inbox: `{result['task_ref']}`"
        return (
            "⚠ Capture failed — check Core logs (capture kill switch off, "
            "Todoist not configured, or inbox project missing)."
        )

    async def on_remember(self, *, text: str, user_id: str) -> str:
        """`/remember <text>` — file a fact about my life, not a task.

        Same idempotency key shape as `/capture`, but the `life_fact` lane:
        Core upserts it into the knowledge store, so re-sending identical
        text just refreshes the same row. Returns a user-facing string.
        """
        text = (text or "").strip()
        if not text:
            return "Usage: `/remember my passport expires in March 2030`"
        ext_id = f"slack:{user_id}:{hashlib.sha256(text.encode()).hexdigest()[:16]}"
        result = await self._core.capture(
            text=text, external_id=ext_id, kind="life_fact"
        )
        if result and result.get("content_id"):
            return f"🧠 Remembered: `{result['content_id'][:12]}`"
        return "⚠ Remember failed — check Core logs (knowledge subsystem unavailable?)."

    async def on_status(self) -> str:
        """Format a `/status` summary from the shared status-digest aggregate.

        Deterministic formatting, NO LLM call — this must be instant and
        free. Failures and pending-on-you first, counts second (issue: the
        old version only showed API/DB/agent-name liveness, not what
        actually ran/broke/spent).
        """
        digest = await self._core.status_digest(hours=24)
        if not digest:
            return "⚠ Could not reach Core API for status."

        lines = ["*System Status* (24h)"]

        failed = digest.get("failed_runs") or []
        soft_failed = digest.get("completed_but_failed") or []
        total_failures = len(failed) + len(soft_failed)
        if total_failures:
            lines.append(f"*Failures: {total_failures}*")
            for r in (failed + soft_failed)[:5]:
                reason = r.get("error") or r.get("reason") or r.get("status") or "unknown"
                lines.append(f"  • {r.get('workflow_type', '?')}: {str(reason)[:140]}")
        else:
            lines.append("Failures: none")

        pending = digest.get("pending_interactions") or 0
        if pending:
            lines.append(f"*Pending on you:* {pending} interaction(s)")

        stuck = digest.get("infra_stuck") or []
        if stuck:
            lines.append(f"*Infra stuck:* {', '.join(stuck[:8])}")

        total_runs = sum(r.get("count", 0) for r in digest.get("runs_by_type_status") or [])
        lines.append(
            f"Runs: {total_runs} | LLM calls: {digest.get('llm_calls', 0)} "
            f"({digest.get('llm_tokens', 0):,} tokens)"
        )
        return "\n".join(lines)

    async def on_file(self, *, file_id: str, channel_id: str, caption: str, client) -> None:
        """Handle a shared file: audio → transcribe+route, PDF → extract+ingest.

        Audio (Slack voice notes / uploads) is transcribed via ElevenLabs Scribe
        and fed into the SAME routing as a typed message — but only in bound
        per-agent channels. PDFs are extracted + ingested then chatted to the
        agent. `client` is the bolt AsyncWebClient (for files_info); the private
        download uses the bot token bearer auth.
        """
        agent_id = self._channel_agent_map.get(channel_id, _DEFAULT_AGENT)
        info = await client.files_info(file=file_id)
        finfo = info.get("file") or {}
        name = finfo.get("name") or "document"
        url = finfo.get("url_private")

        if _is_audio_file(name, (finfo.get("mimetype") or "").lower()):
            await self._handle_audio_file(
                name=name, url=url, channel_id=channel_id, caption=caption
            )
            return

        if not name.lower().endswith(".pdf"):
            await self._adapter.send_message(
                agent_id=agent_id,
                text=f"Unsupported file type: {name}. Only PDF and audio are supported.",
                target={"channel": channel_id},
            )
            return

        extracted = await self._download_and_extract_pdf(url)
        if not extracted:
            await self._adapter.send_message(
                agent_id=agent_id,
                text="Could not extract text from PDF.",
                target={"channel": channel_id},
            )
            return

        # Attach the full extracted text back as a .txt so the user has the
        # complete document, not just the summary below.
        txt_name = (name.rsplit(".", 1)[0] or "document") + ".txt"
        await self._adapter.send_document(
            agent_id=agent_id,
            documents=[{"filename": txt_name, "content": extracted}],
            caption=f"Extracted text from {name}",
            target={"channel": channel_id},
        )

        content_id = await self._core.knowledge_ingest(
            url=f"slack://document/{name}",
            title=name,
            raw_text=extracted,
            tags=[agent_id],
        )
        id_tag = f" (content_id: {content_id})" if content_id else ""
        excerpt = extracted[:8000]
        truncated = len(extracted) > 8000
        text = f"[Document: {name}]{id_tag}"
        if truncated:
            text += f" ({len(extracted)} chars total)"
        text += f"\n\n{excerpt}"
        if truncated:
            text += "\n\n[Full document available via search_knowledge]"
        text += (
            "\n\nPlease summarize the key terms: parties, dates, financial "
            "terms (exact amounts/rates), obligations, restrictions, and "
            "termination conditions."
        )
        if caption:
            text += f"\n\nAdditional context from user: {caption}"

        result = await self._core.chat(
            agent_id=agent_id,
            message=text,
            thread_id=f"slack-{channel_id}-{agent_id}",
            delivery_ref={"adapter": "slack", "channel": channel_id},
        )
        reply = result.get("response", "No response from agent.")
        await self._adapter.send_message(
            agent_id=agent_id, text=reply, target={"channel": channel_id}
        )

    async def _handle_audio_file(
        self, *, name: str, url: str | None, channel_id: str, caption: str
    ) -> None:
        """Transcribe a Slack voice note and route it like a typed message.

        Bound-channels-only: audio in unbound (front-door) channels is ignored so
        intent-routed channels don't pick up stray voice uploads.
        """
        # Unbound / front-door channel → not a per-agent channel → ignore audio.
        agent_id = self._channel_agent_map.get(channel_id)
        if agent_id is None:
            logger.info("slack_audio_ignored_unbound_channel", channel_id=channel_id)
            return

        if not self._elevenlabs_api_key:
            await self._adapter.send_message(
                agent_id=agent_id,
                text="🎤 Voice notes need ElevenLabs configured (AEGIS_ELEVENLABS_API_KEY).",
                target={"channel": channel_id},
            )
            return

        audio = await self._download_private_file(url)
        if not audio:
            await self._adapter.send_message(
                agent_id=agent_id,
                text="Could not download the voice note.",
                target={"channel": channel_id},
            )
            return

        from aegis_comms import elevenlabs

        transcript = await elevenlabs.transcribe(
            audio,
            api_key=self._elevenlabs_api_key,
            model_id=self._elevenlabs_stt_model,
            filename=name,
        )
        if not transcript:
            await self._adapter.send_message(
                agent_id=agent_id,
                text="Could not transcribe the voice note.",
                target={"channel": channel_id},
            )
            return

        # Echo what was heard so STT mishears are visible, then route it as text.
        await self._adapter.send_message(
            agent_id=agent_id,
            text=f"🎤 <i>{transcript}</i>",
            target={"channel": channel_id},
        )
        message = f"{transcript}\n\n{caption}" if caption else transcript
        await self._route_and_dispatch(channel_id=channel_id, text=message)

    async def _download_private_file(self, url: str | None) -> bytes | None:
        """Download a Slack private file via the bot-token bearer auth."""
        if not url:
            return None
        try:
            headers = {"Authorization": f"Bearer {self._bot_token}"}
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                return resp.content
        except Exception as exc:  # noqa: BLE001
            logger.warning("slack_private_download_failed", error=str(exc))
            return None

    async def _download_and_extract_pdf(self, url: str | None) -> str | None:
        """Download a Slack private file and extract PDF text (off-loop)."""
        content = await self._download_private_file(url)
        if not content:
            return None
        try:
            import asyncio
            import io

            from pdfminer.high_level import extract_text

            text = await asyncio.to_thread(extract_text, io.BytesIO(content))
            return text.strip() if text and text.strip() else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("slack_pdf_extraction_failed", error=str(exc))
            return None
