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
  (d) otherwise → sync `/api/chat` with the channel's agent; an unbound
      channel asks core's front door (POST /api/chat/route) who it is for.

Every agent-specific input — aliases, async dispatch, the default agent — is
read from the active agents' rows (GET /api/agents), never from a list of
example ids (#579).

A reply inside a thread is checked against the task sessions first (GET
/api/admin/task-sessions/by-thread): a task's thread is its conversation, so
the reply becomes a note on the task and never reaches (a)-(d).

Core-call contracts match the bot's: POST /api/chat (sync),
POST /api/chat/agent-reply/trigger (async), POST /api/admin/capture (/capture),
POST /api/interactions/{id}/resolve (button), GET /api/health + /api/agents
(/status), POST /api/knowledge/ingest + PATCH the assistant row's delivery-ref,
GET /api/admin/task-sessions/by-thread + POST /api/admin/tasks/{id}/comment.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, NamedTuple
from urllib.parse import quote, urlencode

import httpx
import structlog

from aegis_comms.adapters.base import DeliveryRef
from aegis_comms.adapters.slack import slack_thread_id
from aegis_comms.errors import error_text

logger = structlog.get_logger()

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


# Spoken openers that mean "file this", not "talk to me about this" (B3). A
# transcript starting with one of these skips chat routing and goes to
# POST /api/admin/capture with kind="auto", which picks the lane.
_CAPTURE_PREFIXES = (
    "note to self",
    "make a note",
    "add to inbox",
    "capture",
    "remember",
)


def capture_intent_text(transcript: str) -> str | None:
    """Strip a capture-intent opener off a transcript, or None if absent.

    The opener must be a whole word — "remembering the milk" is a sentence
    about remembering, not a capture instruction, and must still reach chat.
    """
    text = (transcript or "").strip()
    lowered = text.lower()
    for prefix in _CAPTURE_PREFIXES:
        if not lowered.startswith(prefix):
            continue
        if len(text) > len(prefix) and (
            text[len(prefix)].isalnum() or text[len(prefix)] == "'"
        ):
            continue  # "remembering ..." — not the bare opener
        return text[len(prefix) :].lstrip(" ,:;.-—") or None
    return None


def capture_ack(result: dict | None) -> str:
    """User-facing acknowledgement for a classified capture."""
    if not result:
        return "⚠ Capture failed — check Core logs."
    if result.get("lane") == "life_fact" and result.get("content_id"):
        return f"🧠 Filed as a life fact: `{str(result['content_id'])[:12]}`"
    if result.get("task_ref"):
        return f"📥 Captured to Inbox: `{result['task_ref']}`"
    return "⚠ Capture failed — check Core logs."


# Front-door conversation stickiness window. An ambiguous follow-up within this
# many seconds stays with the conversation's last agent.
# ponytail: fixed 30-min TTL; per-user tuning only if it ever matters.
_STICKY_TTL_SECONDS = 1800.0

# How long the routing config derived from GET /api/agents is cached before
# re-fetching. Short so admin Behavior-tab edits apply within a minute without
# a comms restart.
_ROUTING_CFG_TTL_SECONDS = 60.0

# The behavior tag whose holder takes a message nobody claims — the same
# generalist core's front door falls back to (`chat.GENERALIST_TAG`).
_GENERALIST_TAG = "gtd"


@dataclass(frozen=True)
class RoutingConfig:
    """What inbound routing knows about the agents, from GET /api/agents.

    All of it comes from the active agents' rows (#579): the `@alias` → id map
    from `metadata.mention_aliases`, the agents dispatched async from
    `metadata.async_dispatch`, and the default agent from the `gtd` capability
    tag. Nothing is keyed on an example id, so a fork that renames its agents
    routes the same way.

    The empty config is what comms has before core has ever answered: no alias
    is recognised, nothing dispatches async and there is no default, so a
    message goes to its channel's agent, or to core's front door to pick one.
    """

    mention_map: dict[str, str] = field(default_factory=dict)
    async_agents: frozenset[str] = frozenset()
    default_agent: str = ""


def _build_mention_re(mention_map: dict[str, str]) -> re.Pattern[str]:
    """Compile the `@<alias>` matcher for a given label→agent map."""
    return re.compile(
        r"(?<![\w])@("
        + "|".join(re.escape(n) for n in sorted(mention_map, key=len, reverse=True))
        + r")\b[:,]?",
        re.IGNORECASE,
    )


def _derive_mention_map(agents: list[dict] | None) -> dict[str, str]:
    """Build the Slack-label → agent-id map from active agents' metadata.

    Each agent contributes its `metadata.mention_aliases` (default `[agent.id]`)
    plus its own id, all lowercased. {} when there are no agents.
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


def _derive_default_agent(agents: list[dict] | None) -> str:
    """Who takes a message nobody claims: the first active agent, by id,
    holding the `gtd` tag. "" when none does — never an example id."""
    holders = sorted(
        str(a["id"])
        for a in (agents or [])
        if a.get("id") and _GENERALIST_TAG in (a.get("capabilities") or [])
    )
    return holders[0] if holders else ""


def _parse_agent_mention(
    text: str, mention_map: dict[str, str] | None = None
) -> tuple[str | None, str]:
    """Detect an `@<agent>` token anywhere in `text` (ported from bot.py).

    Returns `(target_agent, stripped_text)` when a known agent is mentioned;
    the mention is removed and surrounding whitespace collapsed so the LLM
    doesn't see a self-reference. First-found wins; `info@mail.com` does not
    false-positive (word-boundary match). `mention_map` is the DB-derived
    alias map; with none, no mention is recognised.
    """
    if not text or not mention_map:
        return None, text
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
    async) are the DB-derived values (`RoutingConfig`); left out, no mention is
    recognised and nothing dispatches async.
    """
    if async_agents is None:
        async_agents = frozenset()
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


def dead_card_text(status: str) -> str:
    """What a card says once core reports it closed without an answer."""
    if status == "retired":
        # A newer card replaced it or its problem resolved (#629). The worker
        # edits the card itself; this covers a tap that beat it, and the
        # reminder copies of an escalating card.
        return (
            "⏭ Retired — a newer card replaced this one, or the problem "
            "resolved on its own. Nothing was done."
        )
    return f"⏰ Expired — this card timed out before a response ({status})"


# One try at saving a typed answer, made BEFORE the modal is acked, so a save
# that is not confirmed leaves the modal open with the text still in it.
# Slack wants the ack within 3 seconds of sending the submission, and the trip
# over the socket each way comes out of that, so the save (the resolve, plus a
# read when the card was already resolved) gets 2.0 s and the rest keeps a
# second of margin. Core's resolve is a SELECT, an UPDATE and a Temporal
# signal, normally a small fraction of the budget.
TEXT_SAVE_BUDGET_S = 2.0

_TEXT_ANSWERED = "✅ Answered"
_TEXT_CLOSED_CARD = (
    "✅ Already answered — this card was closed before your answer arrived, "
    "so your answer was not recorded."
)
_TEXT_TRY_AGAIN = "Could not save just now. Press Send again."
_TEXT_KEEP_IT = " Copy your answer if you want to keep it."


class _TextOutcome(NamedTuple):
    """What a save attempt means for the modal and for the card.

    `error` non-empty keeps the modal open with that message; `card`
    non-empty is what the card is edited to after the ack. `reason` is for
    the log. None of the three ever holds the answer text.
    """

    reason: str
    error: str = ""
    card: str = ""


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

    async def _request(
        self,
        method: str,
        path: str,
        *,
        non_ok_event: str | None,
        failed_event: str,
        json: dict | None = None,
        timeout: float,
        ok: tuple[int, ...] = (200,),
        error_sink: dict | None = None,
    ) -> Any:
        """One call to Core. Returns the JSON body, or None on any failure.

        The two log event names are the CALLER's, not this function's. A log
        event name is an interface — Loki queries and dashboards select on it —
        so `_post`, `_patch` and `_get` each keep the exact names they emitted
        before they shared a body. `non_ok_event=None` is a read whose "not
        there" answer is normal and logged nothing.

        Pass `error_sink` to also capture WHY it failed (`reason` key) — Core
        puts the real cause (LLM auth error, tool crash, …) in the 500 body,
        and callers that report to a human should say that rather than a
        generic "couldn't reach Core". A non-ok response also fills in
        `status_code` (int) so a caller can tell a deterministic 4xx (never
        worth retrying) from a transient 5xx/transport failure (issue #296);
        a transport failure leaves `status_code` unset.
        """
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(
                    method,
                    f"{self._core_url}{path}",
                    json=json,
                    auth=self._auth,
                    headers=self._headers(),
                )
                if resp.status_code in ok:
                    return resp.json()
                if non_ok_event:
                    logger.warning(
                        non_ok_event,
                        path=path,
                        status=resp.status_code,
                        body=resp.text[:200],
                    )
                if error_sink is not None:
                    error_sink["reason"] = f"Core API returned {resp.status_code}: {resp.text[:400]}"
                    error_sink["status_code"] = resp.status_code
        except Exception as exc:  # noqa: BLE001 — best-effort; caller degrades
            logger.warning(
                failed_event,
                path=path,
                error=error_text(exc, 500),
                error_type=type(exc).__name__,
            )
            if error_sink is not None:
                error_sink["reason"] = f"Could not reach Core API: {type(exc).__name__}: {exc}"
        return None

    async def _post(
        self, path: str, data: dict, timeout: float = 90, error_sink: dict | None = None
    ) -> Any:
        """POST to Core. 202 counts: the async dispatch lane answers with one."""
        return await self._request(
            "POST",
            path,
            non_ok_event="slack_core_post_non_2xx",
            failed_event="slack_core_post_failed",
            json=data,
            timeout=timeout,
            ok=(200, 202),
            error_sink=error_sink,
        )

    async def _patch(self, path: str, data: dict, timeout: float = 30) -> Any:
        return await self._request(
            "PATCH",
            path,
            non_ok_event="slack_core_patch_non_200",
            failed_event="slack_core_patch_failed",
            json=data,
            timeout=timeout,
        )

    async def _get(self, path: str, timeout: float = 15) -> Any:
        return await self._request(
            "GET",
            path,
            non_ok_event=None,
            failed_event="slack_core_get_failed",
            timeout=timeout,
        )

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
        """POST /api/chat/route → {agent_id, method}.

        On failure, or when core has nobody to route to, `agent_id` is "" —
        never an example id (#579). The caller then uses its own copy of the
        `gtd` holder, or sends the message with no agent for core to route.
        """
        result = await self._post("/api/chat/route", {"message": message}, timeout=30)
        if isinstance(result, dict) and result.get("agent_id"):
            return {"agent_id": result["agent_id"], "method": result.get("method", "llm")}
        return {"agent_id": "", "method": "default"}

    async def agent_reply_trigger(
        self,
        *,
        target_agent: str,
        message: str,
        thread_id: str,
        reply_chat_id: int,
        reply_ref: dict | None = None,
    ) -> dict | None:
        """POST /api/chat/agent-reply/trigger — spawn AgentChatReplyFlow (async).

        `reply_ref` is where the answer goes: `{"channel"}` for the channel the
        question was asked in, plus `"ts"` when it was asked inside a thread.
        Omitted from the body when None, so an older core sees the request it
        always did."""
        payload = {
            "target_agent": target_agent,
            "message": message,
            "thread_id": thread_id,
            "reply_chat_id": reply_chat_id,
        }
        if reply_ref:
            payload["reply_ref"] = reply_ref
        return await self._post("/api/chat/agent-reply/trigger", payload, timeout=15)

    async def capture(
        self, *, text: str, external_id: str, kind: str = "task"
    ) -> dict | None:
        """POST /api/admin/capture — Todoist Inbox task, or a `life_fact`.

        `kind="life_fact"` files the text in the knowledge store instead of
        Todoist; the response carries `content_id` rather than `task_ref`.
        `kind="auto"` lets core's intent classifier pick between the two (it
        degrades to the task lane on any LLM fault); the response's `lane`
        says which one it took.
        """
        payload = {
            "text": text[:2000],
            "source": "slack",
            "external_id": external_id,
            "kind": kind,
        }
        return await self._post("/api/admin/capture", payload, timeout=30)

    async def resolve_interaction(
        self,
        *,
        interaction_id: str,
        value: str,
        note: str = "",
        error_sink: dict | None = None,
    ) -> dict | None:
        """POST /api/interactions/{id}/resolve — record a button choice.

        `note` — optional human reason typed into the card's note input; the
        core learning loop (record_correction_from_interaction) turns it into
        a durable agent_memory lesson. `error_sink` — see `_post`; lets
        `on_action` distinguish a permanent 4xx from a retryable failure."""
        response: dict = {"value": value}
        if note:
            response["note"] = note[:500]
        return await self._post(
            f"/api/interactions/{interaction_id}/resolve",
            {"response": response},
            timeout=30,
            error_sink=error_sink,
        )

    async def get_interaction(self, interaction_id: str) -> dict | None:
        """GET /api/interactions/{id} — one card, its status and stored response.

        None on any failure. The body is never logged: it can hold the answer.
        """
        return await self._get(f"/api/interactions/{interaction_id}")

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

    async def task_by_thread(self, channel: str, ts: str) -> str | None:
        """GET /api/admin/task-sessions/by-thread — the task owning a thread root.

        None means "route this message normally". It covers both a miss (the
        route answers 200 with `task_id: null`, which is the common case — most
        threads are not task threads) and any failure, so a Core hiccup costs a
        message its task lane rather than dropping it.
        """
        query = urlencode({"channel": channel, "ts": ts})
        result = await self._get(f"/api/admin/task-sessions/by-thread?{query}")
        if isinstance(result, dict) and result.get("task_id"):
            return str(result["task_id"])
        return None

    async def task_comment(self, task_id: str, text: str) -> bool:
        """POST /api/admin/tasks/{id}/comment — the reply, as a Todoist note.

        Returns whether the note actually landed. The route answers **200 with
        `{"ok": false}`** when Todoist rejects the write, so the body's own flag
        is the answer — a non-None body is not success, and reading it as one
        would swallow the user's reply in silence.
        """
        body = await self._post(
            f"/api/admin/tasks/{quote(task_id, safe='')}/comment",
            {"text": text},
            timeout=30,
        )
        return bool((body or {}).get("ok"))

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

    async def story_feedback(self, *, channel: str, ts: str, reaction: str) -> bool:
        """POST /api/admin/research/story-feedback — True when core took the
        reaction as a verdict on an area story (#675)."""
        result = await self._post(
            "/api/admin/research/story-feedback",
            {"channel": channel, "ts": ts, "reaction": reaction},
            timeout=10,
        )
        return bool(isinstance(result, dict) and result.get("matched"))


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
        # The routing config derived from GET /api/agents (last good read).
        self._routing_cfg: RoutingConfig | None = None
        self._routing_cfg_ts: float = 0.0

    async def _routing_config(self) -> RoutingConfig:
        """The routing config derived from the active agents' rows, cached for
        `_ROUTING_CFG_TTL_SECONDS`.

        When GET /api/agents fails, the last config core gave is kept and the
        next message asks again; before core has ever answered it is the empty
        `RoutingConfig`. Routing never crashes, and never falls back to a list
        of example ids (#579)."""
        now = time.monotonic()
        if self._routing_cfg is not None and now - self._routing_cfg_ts < _ROUTING_CFG_TTL_SECONDS:
            return self._routing_cfg
        try:
            agents = await self._core.agents()
        except Exception as exc:  # noqa: BLE001 — routing must never break inbound
            logger.warning("slack_routing_config_fetch_failed", error=error_text(exc))
            agents = None
        if not isinstance(agents, list):
            return self._routing_cfg or RoutingConfig()
        self._routing_cfg = RoutingConfig(
            mention_map=_derive_mention_map(agents),
            async_agents=frozenset(_derive_async_agents(agents)),
            default_agent=_derive_default_agent(agents),
        )
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
        self,
        *,
        agent_id: str,
        clean_text: str,
        thread_id: str,
        channel_id: str,
        reply_thread: str = "",
    ) -> str:
        """Sync chat: POST /api/chat → post reply → attach delivery-ref.

        Shared by the sync branch and the async-trigger-failed fallback
        (mirrors bot.py::_send_chat + the two-step delivery-ref attach).

        `agent_id` may be "": core's front door then picks the agent and its
        answer names it (#579). Returns the agent that answered ("" if none).
        `reply_thread` is the root of the thread the question was asked in,
        "" for a top-level question.
        """
        result = await self._core.chat(
            agent_id=agent_id,
            message=clean_text,
            thread_id=thread_id,
            delivery_ref={"adapter": "slack", "channel": channel_id},
        )
        reply_text = result.get("response", "No response from agent.")
        assistant_message_id = result.get("assistant_message_id")
        answered_by = result.get("agent_id") or agent_id

        send_result = await self._reply(answered_by, channel_id, reply_text, reply_thread)

        if assistant_message_id and send_result.ok and send_result.ref is not None:
            await self._core.attach_delivery_ref(
                message_id=assistant_message_id,
                delivery_ref=send_result.ref.to_dict(),
            )
        return answered_by

    async def on_message(
        self,
        *,
        channel_id: str,
        text: str,
        user_id: str | None,
        bot_id: str | None = None,
        ts: str = "",
        thread_ts: str = "",
    ) -> None:
        """Route a text message (sync chat vs async agent-reply).

        Ignores the bot's own messages (a `bot_id` is present, or `user_id`
        equals our bot user id) to avoid self-reply loops.

        Task threads come first: a reply inside the thread of a task's coding
        session is that task's next turn, not a chat message, so it is filed as
        a Todoist note and nothing is routed to an agent. Slack sets
        `thread_ts == ts` on a thread ROOT, which is a top-level message and not
        a reply. A thread no task owns falls through to normal routing.

        Note-to-self short-circuit: in the configured note-to-self channel, the
        OWNER's own messages are filed as life facts instead of being routed to
        an agent. Anyone else posting there still gets normal chat routing, and
        so does an @mention of the bot — a note channel must not make the bot
        unreachable in it.

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

        if (
            thread_ts
            and thread_ts != ts
            and await self._handle_task_thread_reply(
                channel_id=channel_id, thread_ts=thread_ts, text=text
            )
        ):
            return

        if self._is_note_to_self(channel_id=channel_id, user_id=user_id, text=text):
            await self._ingest_self_signal(channel_id=channel_id, ts=ts, text=text)
            return

        await self._route_and_dispatch(
            channel_id=channel_id, text=text, ts=ts, thread_ts=thread_ts
        )

    # --- task threads (a task's coding session, one Slack thread) ------------

    async def _handle_task_thread_reply(
        self, *, channel_id: str, thread_ts: str, text: str
    ) -> bool:
        """Try to file a threaded reply as a note on the thread's task.

        Returns True when the thread belongs to a task and the reply was dealt
        with — including when the note was rejected, because that reply is the
        task's, and re-routing it to an agent would answer it twice over. A
        rejection is reported in the thread rather than dropped: the user typed
        it there and nowhere else.

        The apology itself is best-effort. It is already the failure path, and
        letting a second Slack failure escape would take down the message
        handler for a reply this method has, by returning True, taken
        responsibility for.
        """
        task_id = await self._core.task_by_thread(channel_id, thread_ts)
        if not task_id:
            return False
        if not await self._core.task_comment(task_id, text):
            logger.warning(
                "slack_task_thread_comment_failed", channel=channel_id, task_id=task_id
            )
            try:
                await self._adapter.post_thread(
                    ref=DeliveryRef("slack", {"channel": channel_id, "ts": thread_ts}),
                    text="Couldn't post that to the task; try again.",
                )
            except Exception as exc:  # noqa: BLE001 — the apology is never fatal
                logger.warning(
                    "slack_task_thread_apology_failed",
                    channel=channel_id,
                    ts=thread_ts,
                    task_id=task_id,
                    error=error_text(exc),
                )
        return True

    # --- curated self-signal ingest (B2) ------------------------------------

    def _is_note_to_self(self, *, channel_id: str, user_id: str | None, text: str = "") -> bool:
        """True only for the OWNER posting in the configured note-to-self channel.

        Fails safe: no owner id configured (or no channel configured) ⇒ False,
        so an unconfigured deployment ingests nothing.

        An @mention of the bot is never a note: filing it would leave the owner
        unable to talk to the bot in that channel at all, and would store the
        raw `<@UBOT> ...` markup as a "fact".
        """
        if not self._owner_member_id or not self._note_to_self_channel:
            return False
        if channel_id != self._note_to_self_channel:
            return False
        if self._bot_user_id and f"<@{self._bot_user_id}" in (text or ""):
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
            # The owner reacting to a message someone else wrote — the bot's
            # own, in practice. Core decides whether it is an area story and
            # the reaction a verdict (#675); nothing is fetched here, so the
            # message body never leaves Slack.
            if channel_id and ts:
                await self._core.story_feedback(channel=channel_id, ts=ts, reaction=reaction)
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
            detail = str(exc)[:200]
            # A PRIVATE channel is the natural home for notes, and the app has
            # historically shipped without `groups:history` — so this fetch 403s
            # `missing_scope` and the whole lane is a silent no-op. Loud + named,
            # not a generic warning nobody connects to the missing scope.
            if "missing_scope" in detail:
                logger.error(
                    "slack_reaction_history_missing_scope",
                    channel=channel_id,
                    error=detail,
                    remedy=(
                        "the Slack app lacks history scope for this channel type: add "
                        "groups:history (private channels) / channels:history (public) "
                        "/ im:history (DMs) to the bot scopes and REINSTALL the app"
                    ),
                )
            else:
                logger.warning("slack_reaction_history_failed", error=detail)
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

        A missing `ts` therefore has to fail CLOSED: `slack://{channel}/` is the
        same url for every note in that channel, so each one would silently
        overwrite the last into a single degenerate row.
        """
        text = (text or "").strip()
        if not text:
            return
        if not ts:
            logger.warning("slack_self_signal_missing_ts", channel=channel_id)
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

    async def _route_and_dispatch(
        self, *, channel_id: str, text: str, ts: str = "", thread_ts: str = ""
    ) -> None:
        """Route an inbound message body and dispatch it (sync chat vs async).

        Shared post-routing core for both typed messages (`on_message`) and
        transcribed voice notes (`on_file` audio branch) so a voice note behaves
        exactly like a typed message: @mention parsing, sticky-agent, and the
        sync/async split all apply identically.

        Two things every agent now does alike (2026-09-22):

        * **The message is acknowledged at once**, with a :eyes: reaction on
          it (`ts`). Only async agents used to say anything before the answer
          ("Routing to @pandora…"), so a sync agent's 13-second tool loop read
          as no reply at all. The async text is kept only for when the
          reaction cannot be added (no `reactions:write` scope yet): an async
          answer can be minutes away, and silence for that long is worse.
        * **The answer goes where the question was asked.** A message inside
          a thread (`thread_ts` set and not its own `ts`) is answered in that
          thread; a top-level one in the channel. The sync reply used to go to
          the channel whatever the thread, and the async one to the agent's
          own channel whatever the channel.
        """
        acked = await self._acknowledge(channel_id, ts)
        # A thread ROOT has thread_ts == ts: that is a top-level message.
        reply_thread = thread_ts if thread_ts and thread_ts != ts else ""
        cfg = await self._routing_config()
        mode, agent_id, clean_text = route_message(
            channel_id,
            text,
            self._channel_agent_map,
            self._bot_user_id,
            mention_map=cfg.mention_map,
            async_agents=cfg.async_agents,
        )
        now = time.monotonic()

        if mode == "route":
            routed = await self._core.route_intent(message=clean_text)
            # "" when the route call failed or core had nobody: comms' own copy
            # of the gtd holder, else no agent at all and core picks (#579).
            routed_agent = routed.get("agent_id") or cfg.default_agent
            method = routed.get("method", "default")
            sticky = self._sticky_get(channel_id, now)
            # Clear keyword → route by content. Ambiguous (llm/default) + a fresh
            # sticky agent → stay with the conversation's agent.
            agent_id = sticky if (method != "keyword" and sticky is not None) else routed_agent
            mode = "async" if agent_id in cfg.async_agents else "sync"

        # Remember the resolved agent as this channel's conversation context so the
        # next ambiguous follow-up sticks (including after an explicit @mention).
        if agent_id:
            self._sticky_set(channel_id, agent_id, now)

        thread_id = slack_thread_id(channel_id, agent_id)

        if mode == "async":
            reply_ref = {"channel": channel_id}
            if reply_thread:
                reply_ref["ts"] = reply_thread
            triggered = await self._core.agent_reply_trigger(
                target_agent=agent_id,
                message=clean_text,
                thread_id=thread_id,
                reply_chat_id=0,
                reply_ref=reply_ref,
            )
            if triggered is not None:
                if not acked:
                    # No reaction to say it arrived, and pandora's kimi tools
                    # can legitimately run minutes: say it in words.
                    await self._reply(
                        agent_id, channel_id, f"🤖 Routing to @{agent_id}…", reply_thread
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
        answered_by = await self._sync_chat(
            agent_id=agent_id,
            clean_text=clean_text,
            thread_id=thread_id,
            channel_id=channel_id,
            reply_thread=reply_thread,
        )
        if answered_by and not agent_id:
            # Core picked the agent: it is now this conversation's.
            self._sticky_set(channel_id, answered_by, now)

    async def on_action(
        self, *, value: str, channel_id: str, message_ts: str, note: str = ""
    ) -> None:
        """Resolve an interaction button and always leave the card in a
        state that tells the truth about what happened (issue #296).

        `value` is the button payload `interaction:{id}:{v}`. `note` is the
        optional free-text from the card's note input — passed through so a
        correction becomes a durable agent lesson.

        Outcomes:
          - status "resolved" (first tap or an idempotent re-tap) → edit the
            card to `✅ {value}`, buttons cleared.
          - any OTHER non-pending status (e.g. "archived" — a card whose
            `timeout_policy=archive` fired, ~12% of all cards in 30d) → the
            card is dead; edit it to an explicit expired state so it stops
            looking tappable, rather than leaving a silent no-op forever.
          - a permanent 4xx (409 base-drift conflict, 404 gone) → resolve is
            attempted exactly ONCE (a deterministic 4xx never heals — same
            lesson as `TodoistConnector.check_sync_status`); a threaded reply
            under the card gives visible feedback while leaving the card's
            buttons intact (a 409 in particular may still be actionable once
            the drift is resolved).
          - transport failure / 5xx → retried up to 3×, then a threaded
            "couldn't reach AEGIS" reply; buttons stay intact so the user can
            retry.
        """
        interaction_id, val = parse_action(value)
        if not interaction_id:
            return

        ref = DeliveryRef("slack", {"channel": channel_id, "ts": message_ts})

        result = None
        error_sink: dict = {}
        for attempt in range(1, 4):
            error_sink = {}
            result = await self._core.resolve_interaction(
                interaction_id=interaction_id, value=val, note=note, error_sink=error_sink
            )
            if result is not None:
                break
            status_code = error_sink.get("status_code")
            if status_code is not None and 400 <= status_code < 500:
                # Permanent client error — retrying a deterministic 4xx never
                # heals; stop immediately instead of burning up to 90s.
                logger.warning(
                    "slack_action_resolve_permanent_failure",
                    interaction_id=interaction_id,
                    status_code=status_code,
                )
                break
            if attempt < 3:
                logger.warning(
                    "slack_action_resolve_retrying",
                    interaction_id=interaction_id,
                    attempt=attempt,
                )

        if result is not None:
            status = result.get("status", "")
            if status == "resolved":
                await self._adapter.edit_card(ref=ref, text=f"✅ {val}")
                return
            # Any other non-pending status is a dead card (timed out before a
            # response, or some other terminal state) — clear the buttons so
            # it stops looking tappable instead of failing silently forever.
            logger.warning(
                "slack_action_resolve_non_pending_status",
                interaction_id=interaction_id,
                status=status,
            )
            await self._adapter.edit_card(ref=ref, text=dead_card_text(status))
            return

        status_code = error_sink.get("status_code")
        if status_code == 409:
            await self._adapter.post_thread(
                ref=ref,
                text=(
                    "⚠️ This card is stale — it changed since it was rendered. "
                    "Check the interaction and try again."
                ),
            )
            return
        if status_code == 404:
            await self._adapter.post_thread(
                ref=ref, text="⚠️ This card no longer exists."
            )
            return
        if status_code is not None and 400 <= status_code < 500:
            await self._adapter.post_thread(
                ref=ref,
                text=f"⚠️ AEGIS rejected this action ({status_code}) — it was not recorded.",
            )
            return

        # Transport failure or 5xx that survived all 3 retries — the tap
        # genuinely never got through; leave the buttons live.
        logger.warning(
            "slack_action_resolve_failed",
            interaction_id=interaction_id,
            result=result,
            status_code=status_code,
        )
        await self._adapter.post_thread(
            ref=ref,
            text=(
                "⚠️ Couldn't reach AEGIS — your tap was not recorded; the "
                "buttons are still active, try again."
            ),
        )

    async def on_text_answer(
        self, *, interaction_id: str, text: str, channel_id: str, message_ts: str, ack
    ) -> None:
        """Save an `input` card's typed answer, then close the modal only if it saved.

        `ack` is the view_submission's ack. It is called exactly once, and only
        after one save attempt bounded by `TEXT_SAVE_BUDGET_S`:

          - saved → `ack()` closes the modal and the card is edited to
            "Answered". The card never quotes the text.
          - not confirmed (core down, slow, or a 5xx) → the modal stays open
            with the text still in it and says to press Send again. There is
            no retry after the modal closes: that is the path that loses text.
          - closed without this answer (answered by someone else, expired,
            gone) → the modal stays open and says the answer was not saved,
            so the text can still be copied; the card says why it is closed.

        The answer is stored as `{"value": text}`, the shape the admin
        textarea sends. The text is private (a diary answer can come through
        here), so no log line carries it: only the id, the length and the
        outcome.
        """
        logger.info("slack_text_answer_submitted", interaction_id=interaction_id, length=len(text))
        try:
            async with asyncio.timeout(TEXT_SAVE_BUDGET_S):
                outcome = await self._save_text_answer(interaction_id, text)
        except TimeoutError:
            outcome = _TextOutcome("timeout", error=_TEXT_TRY_AGAIN)
        if outcome.error:
            logger.warning(
                "slack_text_answer_not_saved",
                interaction_id=interaction_id,
                reason=outcome.reason,
            )
            await ack(response_action="errors", errors={"answer": outcome.error})
        else:
            await ack()
        if outcome.card:
            await self._adapter.edit_card(
                ref=DeliveryRef("slack", {"channel": channel_id, "ts": message_ts}),
                text=outcome.card,
            )

    async def _save_text_answer(self, interaction_id: str, text: str) -> _TextOutcome:
        """One resolve, and one read when core says the card was already resolved.

        "Already resolved" has two causes that must read differently: an
        earlier Send of this same answer that timed out here but landed at
        core (saved), or an answer from somewhere else (closed). The stored
        value decides. The same words from the admin page count as saved,
        because they are the same answer. So does a stored value blanked to
        `{"value": "", "filed": <note>}`: only the journal prompt's card is
        blanked, once its answer is in the vault, and only one person answers
        it, so that blank is this answer, saved and filed.
        """
        error_sink: dict = {}
        result = await self._core.resolve_interaction(
            interaction_id=interaction_id, value=text, error_sink=error_sink
        )
        if result is None:
            status_code = error_sink.get("status_code")
            if status_code == 404:
                return _TextOutcome(
                    "gone",
                    error="This card no longer exists, so your answer was not saved." + _TEXT_KEEP_IT,
                )
            if status_code is not None and 400 <= status_code < 500:
                return _TextOutcome(
                    f"refused_{status_code}",
                    error="AEGIS refused this answer, so it was not saved." + _TEXT_KEEP_IT,
                )
            return _TextOutcome("unreachable", error=_TEXT_TRY_AGAIN)

        status = result.get("status", "")
        if status != "resolved":
            return _TextOutcome(
                status or "closed",
                error="This card has closed, so your answer was not saved." + _TEXT_KEEP_IT,
                card=dead_card_text(status),
            )
        if not result.get("already_resolved"):
            return _TextOutcome("saved", card=_TEXT_ANSWERED)

        stored = await self._core.get_interaction(interaction_id)
        if stored is None:
            return _TextOutcome("unconfirmed", error=_TEXT_TRY_AGAIN)
        response = stored.get("response")
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except ValueError:
                response = None
        if isinstance(response, dict) and response.get("value") == text:
            return _TextOutcome("saved_earlier", card=_TEXT_ANSWERED)
        if isinstance(response, dict) and response.get("filed"):
            return _TextOutcome("saved_and_filed", card=_TEXT_ANSWERED)
        return _TextOutcome(
            "answered_elsewhere",
            error="This card was already answered, so your answer was not saved." + _TEXT_KEEP_IT,
            card=_TEXT_CLOSED_CARD,
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
        # The channel's agent; elsewhere the gtd holder, or "" — core's front
        # door then picks one and says who (#579).
        agent_id = (
            self._channel_agent_map.get(channel_id)
            or (await self._routing_config()).default_agent
        )
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
            await self._reply(
                agent_id,
                channel_id,
                f"Unsupported file type: {name}. Only PDF and audio are supported.",
            )
            return

        extracted = await self._download_and_extract_pdf(url)
        if not extracted:
            await self._reply(agent_id, channel_id, "Could not extract text from PDF.")
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
            tags=[agent_id] if agent_id else [],
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
            thread_id=slack_thread_id(channel_id, agent_id),
            delivery_ref={"adapter": "slack", "channel": channel_id},
        )
        reply = result.get("response", "No response from agent.")
        await self._reply(result.get("agent_id") or agent_id, channel_id, reply)

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
            await self._reply(
                agent_id,
                channel_id,
                "🎤 Voice notes need ElevenLabs configured (AEGIS_ELEVENLABS_API_KEY).",
            )
            return

        audio = await self._download_private_file(url)
        if not audio:
            await self._reply(agent_id, channel_id, "Could not download the voice note.")
            return

        from aegis_comms import elevenlabs

        transcript = await elevenlabs.transcribe(
            audio,
            api_key=self._elevenlabs_api_key,
            model_id=self._elevenlabs_stt_model,
            filename=name,
        )
        if not transcript:
            await self._reply(agent_id, channel_id, "Could not transcribe the voice note.")
            return

        # Echo what was heard so STT mishears are visible, then route it as text.
        await self._reply(agent_id, channel_id, f"🎤 <i>{transcript}</i>")

        # "remember …" / "note to self …" is a filing instruction, not a
        # conversation opener: hand it to core's intent classifier instead of
        # an agent, and echo which lane it landed in.
        spoken_capture = capture_intent_text(transcript)
        if spoken_capture:
            ext_id = (
                f"slack:{channel_id}:"
                f"{hashlib.sha256(spoken_capture.encode()).hexdigest()[:16]}"
            )
            result = await self._core.capture(
                text=spoken_capture, external_id=ext_id, kind="auto"
            )
            await self._reply(agent_id, channel_id, capture_ack(result))
            return

        message = f"{transcript}\n\n{caption}" if caption else transcript
        await self._route_and_dispatch(channel_id=channel_id, text=message)

    async def _reply(self, agent_id: str, channel_id: str, text: str, thread_ts: str = ""):
        """Say `text` in `channel_id` as `agent_id`, inside the thread rooted
        at `thread_ts` when one is given.

        Every reply this handler sends goes out this way. The adapter's result
        is returned for the one caller that attaches a delivery ref to it.
        """
        target = {"channel": channel_id}
        if thread_ts:
            target["thread_ts"] = thread_ts
        return await self._adapter.send_message(agent_id=agent_id, text=text, target=target)

    async def _acknowledge(self, channel_id: str, ts: str) -> bool:
        """React :eyes: on the message the moment it arrives, for every agent.
        False when there is no message to react to or Slack refused (e.g. the
        app has not been granted `reactions:write`); never raises."""
        if not ts:
            return False
        add = getattr(self._adapter, "add_reaction", None)
        if add is None:
            return False
        return bool(await add(channel=channel_id, ts=ts, name="eyes"))

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
            logger.warning("slack_private_download_failed", error=error_text(exc, 500))
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
            logger.warning("slack_pdf_extraction_failed", error=error_text(exc, 500))
            return None
