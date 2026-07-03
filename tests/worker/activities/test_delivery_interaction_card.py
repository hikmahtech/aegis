"""send_interaction_card POSTs a channel-neutral CardSpec to /api/deliver/card.

Keyboard/Block-Kit rendering moved OUT of the worker and into the comms
package (covered by the comms card tests). This activity now
just forwards the neutral spec; these tests assert the neutral POST body +
endpoint, plus the unconfigured-url guard.
"""

from __future__ import annotations

import json

import pytest
import respx
from aegis_worker.activities.delivery import DeliveryActivities
from httpx import Response
from temporalio.testing import ActivityEnvironment


@pytest.fixture
def delivery():
    return DeliveryActivities(comms_url="http://comms:9000", api_key="test-key", channel="slack")


@pytest.mark.asyncio
@respx.mock
async def test_card_posts_neutral_spec_to_card_endpoint(delivery):
    route = respx.post("http://comms:9000/api/deliver/card").mock(
        return_value=Response(200, json={"ok": True, "message_id": 42})
    )
    env = ActivityEnvironment()
    result = await env.run(
        delivery.send_interaction_card,
        "ia-1",
        "sebas",
        "approval",
        "Reply to proceed",
        None,
    )
    assert result["ok"] is True
    assert result["message_id"] == 42
    body = json.loads(route.calls.last.request.content.decode())
    assert body == {
        "interaction_id": "ia-1",
        "agent_id": "sebas",
        "kind": "approval",
        "prompt": "Reply to proceed",
        "options": None,
        "allow_hint": False,
    }


@pytest.mark.asyncio
@respx.mock
async def test_card_forwards_options_and_kind(delivery):
    route = respx.post("http://comms:9000/api/deliver/card").mock(
        return_value=Response(200, json={"ok": True, "message_id": 43})
    )
    env = ActivityEnvironment()
    await env.run(
        delivery.send_interaction_card,
        "ia-2",
        "sebas",
        "choice",
        "Pick one",
        {"approve": "Do it", "archive": "Archive", "snooze_1d": "Later"},
    )
    body = json.loads(route.calls.last.request.content.decode())
    assert body["kind"] == "choice"
    assert body["options"] == {"approve": "Do it", "archive": "Archive", "snooze_1d": "Later"}
    assert body["interaction_id"] == "ia-2"


@pytest.mark.asyncio
@respx.mock
async def test_card_body_omits_chat_and_topic(delivery):
    """The neutral CardSpec no longer carries chat_id/topic_id — the active
    adapter routes by the agent's channel."""
    route = respx.post("http://comms:9000/api/deliver/card").mock(
        return_value=Response(200, json={"ok": True, "message_id": 44})
    )
    env = ActivityEnvironment()
    await env.run(
        delivery.send_interaction_card,
        "ia-3",
        "sebas",
        "input",
        "Enter text",
        {"aegis_ui_url": "https://aegis.example.com"},
    )
    body = json.loads(route.calls.last.request.content.decode())
    assert "chat_id" not in body
    assert "topic_id" not in body
    assert body["prompt"] == "Enter text"


@pytest.mark.asyncio
@respx.mock
async def test_card_prompt_passed_through_unmodified(delivery):
    """Composed HTML (e.g. <b>label</b>) must reach the service unmodified —
    the channel applies its own markup; the worker no longer touches it."""
    route = respx.post("http://comms:9000/api/deliver/card").mock(
        return_value=Response(200, json={"ok": True, "message_id": 45})
    )
    env = ActivityEnvironment()
    await env.run(
        delivery.send_interaction_card,
        "ia-4",
        "sebas",
        "ack",
        "Gmail auth expired for <b>personal</b>.",
        None,
    )
    body = json.loads(route.calls.last.request.content.decode())
    assert body["prompt"] == "Gmail auth expired for <b>personal</b>."


@pytest.mark.asyncio
async def test_no_comms_returns_web_ref():
    # Web channel (default) / no comms URL → the card lands in the admin inbox;
    # the activity returns a web delivery_ref rather than an error.
    d = DeliveryActivities(comms_url="", api_key="")
    env = ActivityEnvironment()
    result = await env.run(
        d.send_interaction_card,
        "ia-x",
        "sebas",
        "approval",
        "Prompt",
        None,
    )
    assert result["ok"] is True
    assert result["delivery_ref"]["adapter"] == "web"
