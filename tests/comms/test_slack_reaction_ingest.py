"""B2 — curated self-signal ingest (Slack reaction / note-to-self).

The point of this feature is the two hard security filters, so most of this
file is about proving they hold:

  1. the REACTING user must be the configured owner;
  2. the message AUTHOR (`item_user`) must be that same owner.

Both are asserted independently — each guard has its own test that fails if
that guard alone is deleted — and every deny-path test also asserts that
`conversations.history` was never called, so a rejected reaction never even
fetches somebody else's message body.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from aegis_comms.slack_inbound import SlackInbound

OWNER = "UOWNER"
OTHER = "USOMEONE_ELSE"
NOTE_CHANNEL = "CNOTES"


class _FakeSlackClient:
    """Stands in for the bolt AsyncWebClient handed to the event handler.

    Records every conversations_history call so the deny paths can assert the
    Slack API was never touched at all.
    """

    def __init__(self, text: str = "my passport expires in March 2030"):
        self.text = text
        self.calls: list[dict] = []

    async def conversations_history(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "ok": True,
            "messages": [{"user": OWNER, "text": self.text, "ts": kwargs["latest"]}],
        }


def _inbound(*, owner=OWNER, saveit="brain", note_channel=""):
    """SlackInbound with mocked core + adapter, plus a fake Slack client."""
    core = AsyncMock()
    core.knowledge_ingest.return_value = "cid-123"
    adapter = AsyncMock()
    inbound = SlackInbound(
        adapter=adapter,
        core=core,
        channel_agent_map={"CSEBAS": "sebas"},
        bot_user_id="UBOT",
        owner_member_id=owner,
        saveit_emoji=saveit,
        note_to_self_channel=note_channel,
    )
    return inbound, core, adapter, _FakeSlackClient()


async def _react(inbound, client, *, reaction="brain", user=OWNER, item_user=OWNER,
                 channel="CSEBAS", ts="1700000000.000100"):
    await inbound.on_reaction(
        reaction=reaction,
        user_id=user,
        item_user=item_user,
        channel_id=channel,
        ts=ts,
        client=client,
    )


# --- GUARD 1: the reacting user must be the owner ---------------------------


async def test_reaction_from_non_owner_does_nothing():
    """Someone else reacting :brain: to MY message must not ingest anything —
    and must not even fetch the message."""
    inbound, core, _adapter, client = _inbound()

    await _react(inbound, client, user=OTHER, item_user=OWNER)

    core.knowledge_ingest.assert_not_awaited()
    assert client.calls == []


# --- GUARD 2: the message author must be the owner --------------------------


async def test_owner_reacting_to_someone_elses_message_does_nothing():
    """I react :brain: to a colleague's message — never ingest their words,
    and never call conversations.history to read them."""
    inbound, core, _adapter, client = _inbound()

    await _react(inbound, client, user=OWNER, item_user=OTHER)

    core.knowledge_ingest.assert_not_awaited()
    assert client.calls == []


async def test_reaction_with_missing_item_user_does_nothing():
    """Slack omits `item_user` for some item types — absent author fails closed."""
    inbound, core, _adapter, client = _inbound()

    await _react(inbound, client, user=OWNER, item_user="")

    core.knowledge_ingest.assert_not_awaited()
    assert client.calls == []


# --- fail-safe when unconfigured --------------------------------------------


async def test_no_owner_configured_ingests_nothing_and_does_not_raise():
    """Unset `slack_owner_member_id` ⇒ the feature is inert. Notably a blank
    owner must NOT match a blank `user`/`item_user`."""
    inbound, core, _adapter, client = _inbound(owner="")

    await _react(inbound, client, user=OWNER, item_user=OWNER)
    await _react(inbound, client, user="", item_user="")

    core.knowledge_ingest.assert_not_awaited()
    assert client.calls == []


# --- happy path -------------------------------------------------------------


async def test_owner_reacting_to_own_message_ingests_one_life_fact():
    inbound, core, _adapter, client = _inbound()

    await _react(inbound, client, ts="1700000000.000100")

    assert len(client.calls) == 1
    hist = client.calls[0]
    assert hist["channel"] == "CSEBAS"
    assert hist["latest"] == "1700000000.000100"
    assert hist["oldest"] == "1700000000.000100"
    assert hist["inclusive"] is True

    core.knowledge_ingest.assert_awaited_once()
    kw = core.knowledge_ingest.await_args.kwargs
    assert kw["source_type"] == "life_fact"
    assert kw["url"] == "slack://CSEBAS/1700000000.000100"
    assert kw["raw_text"] == "my passport expires in March 2030"
    assert kw["title"] == "my passport expires in March 2030"
    assert "life_fact" in kw["tags"] and "slack" in kw["tags"]


async def test_duplicate_reaction_produces_the_same_url():
    """Idempotency lives in the knowledge layer (content_id = hash(url)), so
    the contract this side must keep is: same message ⇒ same url."""
    inbound, core, _adapter, client = _inbound()

    await _react(inbound, client, ts="1700000000.000100")
    await _react(inbound, client, ts="1700000000.000100")

    assert core.knowledge_ingest.await_count == 2
    urls = {c.kwargs["url"] for c in core.knowledge_ingest.await_args_list}
    assert urls == {"slack://CSEBAS/1700000000.000100"}


# --- emoji / item-shape filtering -------------------------------------------


async def test_unconfigured_emoji_is_ignored():
    inbound, core, _adapter, client = _inbound()

    await _react(inbound, client, reaction="thumbsup")

    core.knowledge_ingest.assert_not_awaited()
    assert client.calls == []


async def test_configured_emoji_set_is_comma_separated_and_colon_tolerant():
    inbound, core, _adapter, client = _inbound(saveit=":memo:, brain")

    await _react(inbound, client, reaction="memo")

    core.knowledge_ingest.assert_awaited_once()


async def test_reaction_on_a_non_message_item_is_ignored():
    """File reactions carry no channel/ts — nothing to fetch or ingest."""
    inbound, core, _adapter, client = _inbound()

    await _react(inbound, client, channel="", ts="")

    core.knowledge_ingest.assert_not_awaited()
    assert client.calls == []


async def test_history_failure_does_not_ingest_or_raise():
    inbound, core, _adapter, _client = _inbound()

    class _Boom:
        async def conversations_history(self, **kwargs):
            raise RuntimeError("slack down")

    await _react(inbound, _Boom())

    core.knowledge_ingest.assert_not_awaited()


async def test_empty_message_body_is_not_ingested():
    inbound, core, _adapter, client = _inbound()
    client.text = "   "

    await _react(inbound, client)

    assert len(client.calls) == 1  # the guards passed; the body was just empty
    core.knowledge_ingest.assert_not_awaited()


# --- note-to-self channel ---------------------------------------------------


async def test_note_to_self_owner_message_is_ingested_not_routed():
    inbound, core, _adapter, _client = _inbound(note_channel=NOTE_CHANNEL)

    await inbound.on_message(
        channel_id=NOTE_CHANNEL, text="renew car insurance in Nov",
        user_id=OWNER, ts="1700000001.000200",
    )

    core.knowledge_ingest.assert_awaited_once()
    kw = core.knowledge_ingest.await_args.kwargs
    assert kw["source_type"] == "life_fact"
    assert kw["url"] == "slack://CNOTES/1700000001.000200"
    assert kw["raw_text"] == "renew car insurance in Nov"
    # ...and it must NOT have been routed to an agent.
    core.chat.assert_not_awaited()
    core.route_intent.assert_not_awaited()
    core.agent_reply_trigger.assert_not_awaited()


async def test_note_to_self_from_another_user_is_routed_normally():
    """A colleague posting in my note-to-self channel is chat, never ingest."""
    inbound, core, _adapter, _client = _inbound(note_channel=NOTE_CHANNEL)
    core.chat.return_value = {"response": "hi", "assistant_message_id": None}
    core.route_intent.return_value = {"agent_id": "sebas", "method": "keyword"}

    await inbound.on_message(
        channel_id=NOTE_CHANNEL, text="hey are you around?",
        user_id=OTHER, ts="1700000002.000300",
    )

    core.knowledge_ingest.assert_not_awaited()
    core.chat.assert_awaited_once()


async def test_note_to_self_unset_channel_routes_normally():
    """No note-to-self channel configured ⇒ my messages still reach agents —
    including an event with a blank channel id, which must not blank-match the
    unset config and get swallowed into the knowledge store."""
    inbound, core, _adapter, _client = _inbound(note_channel="")
    core.chat.return_value = {"response": "hi", "assistant_message_id": None}
    core.route_intent.return_value = {"agent_id": "sebas", "method": "keyword"}

    await inbound.on_message(
        channel_id="CSEBAS", text="what's on today?", user_id=OWNER, ts="1700000003.1"
    )
    await inbound.on_message(
        channel_id="", text="what's on today?", user_id=OWNER, ts="1700000003.2"
    )

    core.knowledge_ingest.assert_not_awaited()
    assert core.chat.await_count == 2


async def test_note_to_self_in_another_channel_routes_normally():
    """A note-to-self channel IS configured, but this message is elsewhere —
    only that one channel may short-circuit into the knowledge store."""
    inbound, core, _adapter, _client = _inbound(note_channel=NOTE_CHANNEL)
    core.chat.return_value = {"response": "hi", "assistant_message_id": None}

    await inbound.on_message(
        channel_id="CSEBAS", text="what's on today?", user_id=OWNER, ts="1700000005.1"
    )

    core.knowledge_ingest.assert_not_awaited()
    core.chat.assert_awaited_once()


async def test_note_to_self_without_owner_configured_routes_normally():
    """Blank owner id must not turn the note channel into a catch-all ingest.

    The dangerous case is a blank-vs-blank identity match: with no owner
    configured, an event carrying no user id must NOT compare equal and get
    ingested. So this drives `user_id=""` as well as a real id.
    """
    inbound, core, _adapter, _client = _inbound(owner="", note_channel=NOTE_CHANNEL)
    core.chat.return_value = {"response": "hi", "assistant_message_id": None}
    core.route_intent.return_value = {"agent_id": "sebas", "method": "keyword"}

    await inbound.on_message(
        channel_id=NOTE_CHANNEL, text="a thought", user_id=OWNER, ts="1700000004.1"
    )
    await inbound.on_message(
        channel_id=NOTE_CHANNEL, text="another thought", user_id="", ts="1700000004.2"
    )

    core.knowledge_ingest.assert_not_awaited()
    assert core.chat.await_count == 2


async def test_note_to_self_whitespace_only_body_is_not_ingested():
    """`on_message` only rejects a fully empty string, so the blank-body guard
    in the ingest helper is what stops a whitespace-only note becoming a row."""
    inbound, core, _adapter, _client = _inbound(note_channel=NOTE_CHANNEL)

    await inbound.on_message(
        channel_id=NOTE_CHANNEL, text="   \n ", user_id=OWNER, ts="1700000006.1"
    )

    core.knowledge_ingest.assert_not_awaited()
    core.chat.assert_not_awaited()  # it WAS treated as a note, just an empty one


async def test_note_to_self_does_not_swallow_an_at_mention_of_the_bot():
    """In a note channel the owner must still be able to TALK to the bot.

    The note lane matched on channel+owner only, so `@AEGIS what's on today?`
    was filed as a life fact — zero chat routing, and the stored "fact" was the
    raw `<@UBOT> ...` markup.
    """
    inbound, core, _adapter, _client = _inbound(note_channel=NOTE_CHANNEL)
    core.chat.return_value = {"response": "three meetings", "assistant_message_id": None}
    core.route_intent.return_value = {"agent_id": "sebas", "method": "keyword"}

    await inbound.on_message(
        channel_id=NOTE_CHANNEL,
        text="<@UBOT> what's on today?",
        user_id=OWNER,
        ts="1700000007.1",
    )

    core.knowledge_ingest.assert_not_awaited()
    core.chat.assert_awaited_once()

    # ...and a plain note in the same channel is still a note.
    await inbound.on_message(
        channel_id=NOTE_CHANNEL, text="passport expires in March", user_id=OWNER, ts="1700000007.2"
    )
    core.knowledge_ingest.assert_awaited_once()


async def test_self_signal_with_no_ts_is_refused_not_collapsed():
    """A missing dedupe key must fail CLOSED.

    `slack://{channel}/{ts}` is the upsert key, so a blank ts makes every note
    in the channel the SAME url — each one silently overwriting the last into a
    single degenerate row rather than erroring.
    """
    inbound, core, _adapter, _client = _inbound(note_channel=NOTE_CHANNEL)

    await inbound.on_message(channel_id=NOTE_CHANNEL, text="first note", user_id=OWNER, ts="")
    await inbound.on_message(channel_id=NOTE_CHANNEL, text="second note", user_id=OWNER, ts="")

    core.knowledge_ingest.assert_not_awaited()


class _RaisingClient:
    def __init__(self, exc: Exception):
        self.exc = exc

    async def conversations_history(self, **kwargs):
        raise self.exc


_MISSING_SCOPE = RuntimeError(
    "The request to the Slack API failed. (url: conversations.history) The server "
    "responded with: {'ok': False, 'error': 'missing_scope', 'needed': "
    "'groups:history', 'provided': 'channels:history'}"
)


async def test_history_missing_scope_is_logged_loudly_with_the_scope_named():
    """A private note channel with no `groups:history` 403s here, and the whole
    lane is a silent no-op — everything looks configured and nothing happens.
    That has to be an ERROR naming the missing scope, not a generic warning
    nobody connects back to the app manifest.
    """
    from structlog.testing import capture_logs

    inbound, core, _adapter, _client = _inbound()

    with capture_logs() as logs:
        await _react(inbound, _RaisingClient(_MISSING_SCOPE))

    core.knowledge_ingest.assert_not_awaited()
    errors = [e for e in logs if e.get("log_level") == "error"]
    assert errors, f"missing_scope must log at ERROR; got {[e.get('log_level') for e in logs]}"
    blob = repr(errors)
    assert "missing_scope" in blob
    assert "groups:history" in blob, "the operator must be told WHICH scope to add"


async def test_an_ordinary_history_failure_stays_a_warning():
    """The ERROR level has to mean something — only missing_scope earns it."""
    from structlog.testing import capture_logs

    inbound, _core, _adapter, _client = _inbound()

    with capture_logs() as logs:
        await _react(inbound, _RaisingClient(RuntimeError("connection reset by peer")))

    assert [e.get("log_level") for e in logs] == ["warning"]


def test_manifest_covers_private_channels():
    """Both B2 lanes are a silent no-op in a PRIVATE channel — which is where a
    note-to-self channel naturally lives — unless the app asks for the `groups`
    scope and event. Without `message.groups` no message event is ever
    delivered, so notes are never filed; without `groups:history` the `:brain:`
    lane's conversations.history fetch 403s. Everything looks configured and
    nothing happens, so the manifest is asserted here rather than trusted.
    """
    import pathlib

    import yaml

    root = pathlib.Path(__file__).resolve().parents[2]
    manifest = yaml.safe_load((root / "comms" / "slack-app-manifest.yaml").read_text())

    scopes = manifest["oauth_config"]["scopes"]["bot"]
    events = manifest["settings"]["event_subscriptions"]["bot_events"]
    assert {"channels:history", "groups:history"} <= set(scopes), scopes
    assert {"message.channels", "message.groups"} <= set(events), events
