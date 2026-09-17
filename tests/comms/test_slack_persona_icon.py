"""The Slack persona icon is `metadata.slack_icon`, never an id lookup (#556).

A fork that renames its agents gets the icons it configures, and an agent with
no icon gets the robot — including one whose id happens to be an example id.
"""

from __future__ import annotations

import httpx
import respx
from aegis_comms.adapters.slack import SlackAdapter


class _S:
    slack_bot_token = "xoxb-test"
    core_url = "http://core.test"
    api_key = ""


def _agent(agent_id: str, metadata: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": agent_id, "name": agent_id.title(), "slack_channel_id": "C1",
            "metadata": metadata,
        },
    )


@respx.mock
async def test_a_renamed_agent_gets_the_icon_it_configures():
    respx.get("http://core.test/api/agents/jeeves").mock(
        return_value=_agent("jeeves", {"slack_icon": ":tophat:"})
    )
    a = SlackAdapter(_S())
    _channel, _username, icon, _voice = await a._resolve("jeeves")
    assert icon == ":tophat:"
    await a.stop()


@respx.mock
async def test_no_icon_is_the_robot_even_for_an_example_id():
    """`raphael` used to get :books: from a table keyed on the id; with no
    `slack_icon` in its metadata it now gets the neutral default."""
    respx.get("http://core.test/api/agents/raphael").mock(return_value=_agent("raphael", {}))
    a = SlackAdapter(_S())
    _channel, _username, icon, _voice = await a._resolve("raphael")
    assert icon == ":robot_face:"
    await a.stop()
