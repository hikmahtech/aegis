
from aegis_comms.adapters.slack import handle_hint_open, handle_hint_submit


class _FakeClient:
    def __init__(self):
        self.opened = None

    async def views_open(self, *, trigger_id, view):
        self.opened = (trigger_id, view)


class _FakeCore:
    def __init__(self):
        self.resolved = None

    async def resolve_interaction(self, *, interaction_id, value):
        self.resolved = (interaction_id, value)
        return {"status": "resolved"}


async def test_hint_open_opens_modal_does_not_resolve():
    client = _FakeClient()
    body = {"trigger_id": "T1", "actions": [{"action_id": "hint_open", "value": "interaction:ID7:hint_open"}],
            "message": {"text": "DB down"}}
    await handle_hint_open(client, body)
    assert client.opened is not None
    trigger_id, view = client.opened
    assert trigger_id == "T1"
    assert view["private_metadata"] == "ID7"


async def test_hint_submit_resolves_with_hint_prefix():
    core = _FakeCore()
    body = {"view": {"callback_id": "hint_submit", "private_metadata": "ID7",
                     "state": {"values": {"hint": {"value": {"value": "acme/news-service"}}}}}}
    await handle_hint_submit(core, body)
    assert core.resolved == ("ID7", "hint:acme/news-service")


async def test_hint_submit_malformed_is_noop():
    core = _FakeCore()
    await handle_hint_submit(core, {"view": {"callback_id": "other"}})
    assert core.resolved is None
