"""B2 wiring: the `reaction_added` bolt handler must actually be registered.

`on_reaction` is fully unit-tested in test_slack_reaction_ingest.py, but that
proves nothing if `start_listener` never subscribes to the event — the feature
would be dead code in production. This drives the real `start_listener` with a
fake `AsyncApp` that records the registered events, then invokes the captured
handler with a real-shaped `reaction_added` payload to prove the adapter maps
`item.channel` / `item.ts` / `item_user` onto `on_reaction` correctly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock


class _FakeApp:
    """Records bolt registrations instead of talking to Slack."""

    def __init__(self, token=None):
        self.events: dict[str, object] = {}

    def event(self, name):
        def deco(fn):
            self.events[name] = fn
            return fn

        return deco

    def action(self, name):
        return lambda fn: fn

    def view(self, name):
        return lambda fn: fn

    def command(self, name):
        return lambda fn: fn


class _FakeSocketHandler:
    def __init__(self, app, token):
        self.app = app

    async def start_async(self):
        return None


async def _listen(monkeypatch, **env):
    """Run SlackAdapter.start_listener against fakes; return (fake_app, inbound)."""
    import aegis_comms.slack_inbound as si
    import slack_bolt.adapter.socket_mode.async_handler as sm
    import slack_bolt.app.async_app as aa

    monkeypatch.setenv("AEGIS_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("AEGIS_SLACK_APP_TOKEN", "xapp-test")
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    from aegis_comms.adapters.slack import SlackAdapter
    from aegis_comms.config import CommsSettings

    apps: list[_FakeApp] = []

    def _mk_app(token=None):
        apps.append(_FakeApp(token))
        return apps[-1]

    monkeypatch.setattr(aa, "AsyncApp", _mk_app)
    monkeypatch.setattr(sm, "AsyncSocketModeHandler", _FakeSocketHandler)

    inbounds: list[object] = []
    real_inbound = si.SlackInbound

    def _mk_inbound(**kw):
        obj = real_inbound(**kw)
        inbounds.append(obj)
        return obj

    monkeypatch.setattr(si, "SlackInbound", _mk_inbound)

    adapter = SlackAdapter(CommsSettings(_env_file=None))
    adapter._client = AsyncMock()
    adapter._client.auth_test.return_value = {"user_id": "UBOT"}
    monkeypatch.setattr(
        SlackAdapter, "_build_channel_agent_map", AsyncMock(return_value={})
    )

    await adapter.start_listener()
    return apps[0], inbounds[0]


async def test_reaction_added_event_is_registered(monkeypatch):
    app, _inbound = await _listen(monkeypatch)
    assert "reaction_added" in app.events


async def test_reaction_handler_maps_the_slack_payload_onto_on_reaction(monkeypatch):
    """A real-shaped reaction_added payload must reach on_reaction with the
    channel/ts pulled out of `item` and the author from `item_user`."""
    app, inbound = await _listen(monkeypatch)
    seen: list[dict] = []

    async def _capture(**kw):
        seen.append(kw)

    inbound.on_reaction = _capture

    await app.events["reaction_added"](
        {
            "type": "reaction_added",
            "user": "UOWNER",
            "reaction": "brain",
            "item_user": "UOWNER",
            "item": {"type": "message", "channel": "C123", "ts": "1700000000.000100"},
            "event_ts": "1700000000.000200",
        },
        "the-client",
    )

    assert len(seen) == 1
    assert seen[0]["reaction"] == "brain"
    assert seen[0]["user_id"] == "UOWNER"
    assert seen[0]["item_user"] == "UOWNER"
    assert seen[0]["channel_id"] == "C123"
    assert seen[0]["ts"] == "1700000000.000100"
    assert seen[0]["client"] == "the-client"


async def test_message_handler_forwards_ts_for_note_to_self(monkeypatch):
    """Note-to-self dedupes on `slack://channel/ts`, so the message handler has
    to pass the event ts through — without it every note collides on one row."""
    app, inbound = await _listen(monkeypatch)
    seen: list[dict] = []

    async def _capture(**kw):
        seen.append(kw)

    inbound.on_message = _capture

    await app.events["message"](
        {"channel": "C123", "text": "a note", "user": "UOWNER", "ts": "1700000009.5"},
        None,
    )

    assert len(seen) == 1
    assert seen[0]["ts"] == "1700000009.5"


async def test_self_capture_settings_are_passed_into_slack_inbound(monkeypatch):
    """The owner id / emoji / channel must be handed to SlackInbound, otherwise
    the guards see blanks and the feature is permanently inert."""
    _app, inbound = await _listen(
        monkeypatch,
        AEGIS_SLACK_OWNER_MEMBER_ID="UOWNER",
        AEGIS_SLACK_SAVEIT_EMOJI="memo",
        AEGIS_SLACK_NOTE_TO_SELF_CHANNEL="CNOTES",
    )
    assert inbound._owner_member_id == "UOWNER"
    assert inbound._saveit_emojis == frozenset({"memo"})
    assert inbound._note_to_self_channel == "CNOTES"
