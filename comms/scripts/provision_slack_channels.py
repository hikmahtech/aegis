"""Idempotent Slack channel provisioning script.

Creates the five AEGIS channels if they don't already exist, then prints a
JSON mapping of {agent_id: channel_id} and a copy-pasteable SQL UPDATE block.

Usage:
    AEGIS_SLACK_BOT_TOKEN=xoxb-... python comms/scripts/provision_slack_channels.py

Core logic lives in `provision(client, target_map)` — testable without a real
Slack workspace (mock the WebClient in unit tests).
"""

from __future__ import annotations

import json
import os

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# Maps agent_id → Slack channel name (without #).
DEFAULT_TARGET_MAP: dict[str, str] = {
    "system": "aegis-general",
    "sebas": "aegis-sebas",
    "raphael": "aegis-raphael",
    "maou": "aegis-maou",
    "pandoras-actor": "aegis-pandora",
}


def _list_all_channels(client: WebClient) -> dict[str, str]:
    """Return name → channel_id for all public channels visible to the bot."""
    name_to_id: dict[str, str] = {}
    cursor: str | None = None
    while True:
        kwargs: dict = {"types": "public_channel", "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        resp = client.conversations_list(**kwargs)
        for ch in resp["channels"]:
            name_to_id[ch["name"]] = ch["id"]
        cursor = resp.get("response_metadata", {}).get("next_cursor") or ""
        if not cursor:
            break
    return name_to_id


def provision(client: WebClient, target_map: dict[str, str]) -> dict[str, str]:
    """Ensure every channel in target_map exists; return {agent_id: channel_id}.

    For each target channel name that does not yet exist, calls
    conversations_create. On `name_taken`, treats it as already existing and
    fetches the id from a fresh listing.

    Args:
        client: An initialised slack_sdk.WebClient (sync).
        target_map: {agent_id: channel_name} mapping to provision.

    Returns:
        {agent_id: channel_id} for all five agents.
    """
    existing = _list_all_channels(client)
    result: dict[str, str] = {}

    for agent_id, channel_name in target_map.items():
        if channel_name in existing:
            result[agent_id] = existing[channel_name]
            continue

        try:
            resp = client.conversations_create(name=channel_name)
            result[agent_id] = resp["channel"]["id"]
        except SlackApiError as exc:
            if exc.response.get("error") == "name_taken":
                # Race: someone created it between our list and create calls.
                # Refresh the listing to get the id.
                existing = _list_all_channels(client)
                result[agent_id] = existing[channel_name]
            else:
                raise

    return result


def _sql_block(agent_channel_map: dict[str, str]) -> str:
    """Return a copy-pasteable SQL UPDATE block for agents.slack_channel_id."""
    lines = ["-- Run against prod DB after provisioning:"]
    for agent_id, channel_id in agent_channel_map.items():
        lines.append(
            f"UPDATE agents SET slack_channel_id = '{channel_id}'"
            f" WHERE id = '{agent_id}';"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    token = os.environ.get("AEGIS_SLACK_BOT_TOKEN")
    if not token:
        raise SystemExit("AEGIS_SLACK_BOT_TOKEN is not set")

    web_client = WebClient(token=token)
    mapping = provision(web_client, DEFAULT_TARGET_MAP)

    print(json.dumps(mapping, indent=2))
    print()
    print(_sql_block(mapping))
