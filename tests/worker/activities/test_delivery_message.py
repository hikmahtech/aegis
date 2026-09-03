"""`send_message` and the per-task Slack thread field.

A task's messages all belong to ONE thread, and the worker's whole share of
that contract is forwarding the thread ROOT ref to comms. Two body shapes are
pinned here: with a `thread_ref` and without. The key must be ABSENT rather
than null when there is no thread — a null would be indistinguishable from a
malformed ref on the comms side, which degrades to an unthreaded post anyway,
so the difference only ever shows up as a silently unthreaded message.
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
async def test_send_message_forwards_the_thread_ref(delivery):
    route = respx.post("http://comms:9000/api/deliver/message").mock(
        return_value=Response(
            200,
            json={
                "ok": True,
                "delivery_ref": {"adapter": "slack", "channel": "C1", "ts": "2.2"},
                "channel": "C1",
                "ts": "2.2",
            },
        )
    )
    env = ActivityEnvironment()
    result = await env.run(
        delivery.send_message,
        "pandoras-actor",
        "turn 2 done",
        0,
        {"channel": "C1", "ts": "1.1"},
    )

    assert json.loads(route.calls.last.request.content.decode()) == {
        "text": "turn 2 done",
        "agent_id": "pandoras-actor",
        "thread_ref": {"channel": "C1", "ts": "1.1"},
    }
    # Returned UNCHANGED: the ref inside it is what the flow stores as the
    # thread root, so anything this activity dropped would be unrecoverable.
    assert result["delivery_ref"] == {"adapter": "slack", "channel": "C1", "ts": "2.2"}


@pytest.mark.asyncio
@respx.mock
async def test_send_message_omits_thread_ref_when_there_is_no_thread(delivery):
    route = respx.post("http://comms:9000/api/deliver/message").mock(
        return_value=Response(200, json={"ok": True})
    )
    env = ActivityEnvironment()
    await env.run(delivery.send_message, "pandoras-actor", "hello")

    assert json.loads(route.calls.last.request.content.decode()) == {
        "text": "hello",
        "agent_id": "pandoras-actor",
    }


@pytest.mark.asyncio
@respx.mock
async def test_send_message_forwards_thread_overflow(delivery):
    """The flag that keeps a thread-opening message's chunks together.

    It travels WITHOUT a `thread_ref` — that is the whole point: the message
    has no root yet because it is about to become one.
    """
    route = respx.post("http://comms:9000/api/deliver/message").mock(
        return_value=Response(200, json={"ok": True})
    )
    env = ActivityEnvironment()
    await env.run(delivery.send_message, "pandoras-actor", "turn 1 done", 0, None, True)

    assert json.loads(route.calls.last.request.content.decode()) == {
        "text": "turn 1 done",
        "agent_id": "pandoras-actor",
        "thread_overflow": True,
    }


@pytest.mark.asyncio
@respx.mock
async def test_send_message_omits_thread_overflow_when_false(delivery):
    """Absent rather than `false`, so every non-task call site sends the body it
    has always sent."""
    route = respx.post("http://comms:9000/api/deliver/message").mock(
        return_value=Response(200, json={"ok": True})
    )
    env = ActivityEnvironment()
    await env.run(
        delivery.send_message, "pandoras-actor", "hi", 0, {"channel": "C1", "ts": "1.1"}, False
    )

    assert "thread_overflow" not in json.loads(route.calls.last.request.content.decode())
