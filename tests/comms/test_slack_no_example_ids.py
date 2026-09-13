"""Comms finds no agent by an example id (#579).

Aliases, async dispatch, the default agent and channel stems all come from
core's agent rows. These tests also pin what each path does when core cannot
be reached.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import aegis_comms
import httpx
import respx
from aegis_comms.__main__ import create_delivery_app
from aegis_comms.adapters.base import SendResult
from aegis_comms.adapters.slack import SlackAdapter
from aegis_comms.config import CommsSettings
from aegis_comms.slack_inbound import RoutingConfig, SlackInbound, _derive_default_agent
from httpx import ASGITransport, AsyncClient

# A fork's agents: nothing here is an example id.
_AGENTS = [
    {"id": "jeeves", "capabilities": ["gtd"], "metadata": {}},
    {
        "id": "ops-bot",
        "capabilities": ["infra"],
        "metadata": {"mention_aliases": ["ops"], "async_dispatch": True},
    },
]


def test_comms_source_names_no_example_agent():
    root = Path(aegis_comms.__file__).parent
    for path in root.rglob("*.py"):
        text = path.read_text()
        for name in ('"sebas"', '"raphael"', '"maou"', '"pandoras-actor"', '"pandora"'):
            assert name not in text, f"{path.relative_to(root)} names {name}"


def test_the_default_agent_is_the_gtd_holder_first_by_id():
    assert _derive_default_agent(_AGENTS) == "jeeves"
    two = [{"id": "b", "capabilities": ["gtd"]}, {"id": "a", "capabilities": ["gtd"]}]
    assert _derive_default_agent(two) == "a"
    assert _derive_default_agent([{"id": "x", "capabilities": []}]) == ""
    assert _derive_default_agent(None) == ""


# --- inbound routing -------------------------------------------------------------


def _inbound(agents):
    core = AsyncMock()
    adapter = AsyncMock()
    core.agents.return_value = agents
    core.chat.return_value = {"response": "ok", "assistant_message_id": None}
    core.agent_reply_trigger.return_value = {"workflow_id": "w"}
    adapter.send_message.return_value = SendResult(ok=True, ref=None, used_html=False)
    inbound = SlackInbound(
        adapter=adapter, core=core, channel_agent_map={"COPS": "ops-bot"}, bot_user_id="U0BOT"
    )
    return inbound, core, adapter


async def test_renamed_agents_route_by_their_rows():
    inbound, core, _ = _inbound(_AGENTS)
    await inbound.on_message(channel_id="CGEN", text="@ops restart it", user_id="U1")
    assert core.agent_reply_trigger.await_args.kwargs["target_agent"] == "ops-bot"
    # Its bound channel dispatches async too, from `metadata.async_dispatch`.
    await inbound.on_message(channel_id="COPS", text="and the logs", user_id="U1")
    assert core.agent_reply_trigger.await_count == 2


async def test_a_failed_route_call_uses_the_gtd_holder_from_the_agent_rows():
    inbound, core, _ = _inbound(_AGENTS)
    core.route_intent.return_value = {"agent_id": "", "method": "default"}
    await inbound.on_message(channel_id="CGEN", text="hi", user_id="U1")
    assert core.chat.await_args.kwargs["agent_id"] == "jeeves"


async def test_core_down_sends_no_agent_and_posts_as_whoever_core_picks():
    """No agent rows and no route: the message goes to core with no agent, core's
    front door picks one, and the conversation sticks to it."""
    inbound, core, adapter = _inbound(None)
    core.route_intent.return_value = {"agent_id": "", "method": "default"}
    core.chat.return_value = {"response": "ok", "assistant_message_id": None, "agent_id": "jeeves"}
    await inbound.on_message(channel_id="CGEN", text="hi", user_id="U1")
    assert core.chat.await_args.kwargs["agent_id"] == ""
    assert adapter.send_message.await_args.kwargs["agent_id"] == "jeeves"

    core.route_intent.return_value = {"agent_id": "", "method": "llm"}
    await inbound.on_message(channel_id="CGEN", text="and?", user_id="U1")
    assert core.chat.await_args.kwargs["agent_id"] == "jeeves"


async def test_before_core_ever_answers_no_alias_is_recognised():
    inbound, core, _ = _inbound(None)
    assert await inbound._routing_config() == RoutingConfig()
    core.route_intent.return_value = {"agent_id": "", "method": "default"}
    await inbound.on_message(channel_id="CGEN", text="@pandora check", user_id="U1")
    core.agent_reply_trigger.assert_not_awaited()
    assert core.chat.await_args.kwargs["message"] == "@pandora check"


async def test_a_failed_read_keeps_the_last_config_core_gave():
    inbound, core, _ = _inbound(_AGENTS)
    first = await inbound._routing_config()
    assert first.default_agent == "jeeves"
    inbound._routing_cfg_ts = 0.0  # expire the cache
    core.agents.return_value = None
    assert await inbound._routing_config() == first


# --- the outbound adapter ------------------------------------------------------------


def _settings() -> CommsSettings:
    return CommsSettings(
        AEGIS_CHANNEL="slack",
        AEGIS_SLACK_BOT_TOKEN="xoxb-test",
        AEGIS_CORE_URL="http://core.test",
        AEGIS_API_KEY="k",
    )


def _channels(*names: str) -> dict:
    return {
        "ok": True,
        "channels": [{"name": n, "id": f"C{n.upper()}"} for n in names],
        "response_metadata": {"next_cursor": ""},
    }


@respx.mock
async def test_no_agent_named_is_the_gtd_holder():
    respx.get("http://core.test/api/agents").mock(return_value=httpx.Response(200, json=_AGENTS))
    respx.get("http://core.test/api/agents/jeeves").mock(
        return_value=httpx.Response(
            200, json={"id": "jeeves", "name": "Jeeves", "slack_channel_id": "CJ", "metadata": {}}
        )
    )
    a = SlackAdapter(_settings())
    assert await a.resolve_agent_id("") == "jeeves"
    channel, username, _icon, _voice = await a._resolve("")
    assert (channel, username) == ("CJ", "Jeeves")
    await a.stop()


@respx.mock
async def test_no_agent_and_core_down_posts_as_aegis_in_the_general_channel():
    respx.get("http://core.test/api/agents").mock(side_effect=httpx.NetworkError("down"))
    respx.get("http://core.test/api/agents/system").mock(side_effect=httpx.NetworkError("down"))
    a = SlackAdapter(_settings())
    a._client = AsyncMock()
    a._client.conversations_list.return_value = _channels("aegis-general")
    assert await a._resolve("") == ("CAEGIS-GENERAL", "AEGIS", ":gear:", "")
    await a.stop()


@respx.mock
async def test_the_channel_stem_is_the_alias_core_gave_even_once_core_is_down():
    respx.get("http://core.test/api/agents").mock(return_value=httpx.Response(200, json=_AGENTS))
    respx.get("http://core.test/api/agents/ops-bot").mock(side_effect=httpx.NetworkError("down"))
    a = SlackAdapter(_settings())
    a._client = AsyncMock()
    a._client.conversations_list.return_value = _channels("aegis-ops")
    await a._build_channel_agent_map()
    channel, *_ = await a._resolve("ops-bot")
    assert channel == "CAEGIS-OPS"
    await a.stop()


async def test_a_delivery_naming_no_agent_goes_to_the_default():
    adapter = AsyncMock()
    adapter.resolve_agent_id.return_value = "jeeves"
    adapter.send_message.return_value = SendResult(ok=True, ref=None, used_html=False)
    app = create_delivery_app(adapter, CommsSettings(_env_file=None, core_url="", api_key=""))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/deliver/message", json={"text": "hi"})
    assert r.json()["agent_id"] == "jeeves"
    assert adapter.send_message.await_args.kwargs["agent_id"] == "jeeves"
