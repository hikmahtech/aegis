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

# Maps agent_id → Slack channel name (without #). Fallback for when the core
# API isn't reachable; the live map is derived from the actual agent set via
# `target_map_from_agents` so a from-scratch/renamed fleet provisions itself.
DEFAULT_TARGET_MAP: dict[str, str] = {
    "system": "aegis-general",
    "sebas": "aegis-sebas",
    "raphael": "aegis-raphael",
    "maou": "aegis-maou",
    "pandoras-actor": "aegis-pandora",
}


def _channel_stem(agent: dict) -> str:
    """`aegis-<stem>` channel stem for an agent: its first mention_alias (so
    pandoras-actor → `pandora`, matching the adapter), else its id."""
    aliases = (agent.get("metadata") or {}).get("mention_aliases") or []
    return str(aliases[0]) if aliases else str(agent.get("id"))


def target_map_from_agents(agents: list[dict]) -> dict[str, str]:
    """Build {agent_id: channel_name} from a `GET /api/agents` payload, so a
    custom agent set provisions without editing this script."""
    out: dict[str, str] = {}
    for a in agents or []:
        aid = a.get("id")
        if aid:
            out[aid] = f"aegis-{_channel_stem(a)}"
    return out or dict(DEFAULT_TARGET_MAP)


def _fetch_agents(core_url: str, api_key: str) -> list[dict]:
    """GET /api/agents from the core API (best-effort; empty list on failure)."""
    import httpx

    headers = {"X-API-Key": api_key} if api_key else {}
    try:
        resp = httpx.get(f"{core_url.rstrip('/')}/api/agents", headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json() or []
    except Exception:  # noqa: BLE001 — fall back to DEFAULT_TARGET_MAP
        return []


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
            f"UPDATE agents SET slack_channel_id = '{channel_id}' WHERE id = '{agent_id}';"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    token = os.environ.get("AEGIS_SLACK_BOT_TOKEN")
    if not token:
        raise SystemExit("AEGIS_SLACK_BOT_TOKEN is not set")

    # Prefer the live agent set when a core URL is given; else the shipped map.
    core_url = os.environ.get("AEGIS_CORE_URL")
    if core_url:
        agents = _fetch_agents(core_url, os.environ.get("AEGIS_API_KEY", ""))
        target_map = target_map_from_agents(agents)
    else:
        target_map = DEFAULT_TARGET_MAP

    web_client = WebClient(token=token)
    mapping = provision(web_client, target_map)

    print(json.dumps(mapping, indent=2))
    print()
    print(_sql_block(mapping))
