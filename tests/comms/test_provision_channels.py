"""Tests for comms/scripts/provision_slack_channels.py.

All Slack API calls are mocked — no live Slack workspace required.
"""

from __future__ import annotations

import os

# Import the module under test.
import sys
from unittest.mock import MagicMock

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "comms", "scripts"),
)
from provision_slack_channels import DEFAULT_TARGET_MAP, provision

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_list_response(*channel_names: str) -> dict:
    """Build a fake conversations_list response with the given channel names."""
    return {
        "channels": [
            {"name": name, "id": f"C_{name.upper()}"}
            for name in channel_names
        ],
        "response_metadata": {"next_cursor": ""},
    }


def _make_create_response(name: str) -> dict:
    return {"channel": {"id": f"C_CREATED_{name.upper()}", "name": name}}


# ---------------------------------------------------------------------------
# Core behaviour tests
# ---------------------------------------------------------------------------

def test_provision_creates_missing_channels():
    """When only aegis-sebas exists, the other 4 must be created."""
    client = MagicMock()
    # First list call returns only aegis-sebas.
    client.conversations_list.return_value = _make_list_response("aegis-sebas")
    # conversations_create returns a fresh id for each missing channel.
    client.conversations_create.side_effect = lambda name: _make_create_response(name)

    result = provision(client, DEFAULT_TARGET_MAP)

    # All 5 agents must appear in the result.
    assert set(result.keys()) == set(DEFAULT_TARGET_MAP.keys())

    # sebas was already there — no create call for it.
    created_names = [c.kwargs["name"] for c in client.conversations_create.call_args_list]
    assert "aegis-sebas" not in created_names

    # The other 4 must have been created.
    assert sorted(created_names) == sorted(
        ["aegis-general", "aegis-raphael", "aegis-maou", "aegis-pandora"]
    )

    # sebas gets the pre-existing id from the list.
    assert result["sebas"] == "C_AEGIS-SEBAS"


def test_provision_idempotent_when_all_present():
    """When all 5 channels already exist, conversations_create must not be called."""
    client = MagicMock()
    client.conversations_list.return_value = _make_list_response(
        "aegis-general",
        "aegis-sebas",
        "aegis-raphael",
        "aegis-maou",
        "aegis-pandora",
    )

    result = provision(client, DEFAULT_TARGET_MAP)

    client.conversations_create.assert_not_called()
    assert set(result.keys()) == set(DEFAULT_TARGET_MAP.keys())
    # Ids come from the listing.
    assert result["system"] == "C_AEGIS-GENERAL"
    assert result["pandoras-actor"] == "C_AEGIS-PANDORA"


def test_provision_returns_correct_ids_for_created_channels():
    """Ids returned for newly created channels come from the create response."""
    client = MagicMock()
    client.conversations_list.return_value = _make_list_response()  # none exist
    client.conversations_create.side_effect = lambda name: _make_create_response(name)

    result = provision(client, DEFAULT_TARGET_MAP)

    for agent_id, channel_name in DEFAULT_TARGET_MAP.items():
        assert result[agent_id] == f"C_CREATED_{channel_name.upper()}"


def test_provision_handles_name_taken_gracefully():
    """On name_taken SlackApiError, re-list and use the existing id."""
    from slack_sdk.errors import SlackApiError

    client = MagicMock()

    # First list: only sebas.
    # Second list (after name_taken): all 5.
    client.conversations_list.side_effect = [
        _make_list_response("aegis-sebas"),
        _make_list_response(
            "aegis-general",
            "aegis-sebas",
            "aegis-raphael",
            "aegis-maou",
            "aegis-pandora",
        ),
    ]

    # Simulate name_taken for the first create attempt (aegis-general).
    err_resp = MagicMock()
    err_resp.get.return_value = "name_taken"
    name_taken_exc = SlackApiError("name_taken", err_resp)

    # First create raises name_taken; subsequent creates succeed.
    create_responses = iter(
        [
            name_taken_exc,
            _make_create_response("aegis-raphael"),
            _make_create_response("aegis-maou"),
            _make_create_response("aegis-pandora"),
        ]
    )

    def _create_side_effect(name):
        val = next(create_responses)
        if isinstance(val, Exception):
            raise val
        return val

    client.conversations_create.side_effect = _create_side_effect

    result = provision(client, DEFAULT_TARGET_MAP)

    # All 5 must be present.
    assert set(result.keys()) == set(DEFAULT_TARGET_MAP.keys())
    # After name_taken we re-listed: aegis-general id comes from the second list.
    assert result["system"] == "C_AEGIS-GENERAL"
