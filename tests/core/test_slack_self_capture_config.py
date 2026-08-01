"""B2 — the self-capture knobs must actually REACH comms.

`slack_saveit_emoji` / `slack_note_to_self_channel` / `slack_owner_member_id`
are admin-Integrations-page settings, which means they live in core's DB and
are overlaid onto core's `Settings`. comms has no DB access, so
`GET /api/internal/slack-config` is the only channel by which it can ever
learn them. Without that, the whole feature is a silent no-op in production
regardless of what the operator types into the admin UI — hence these tests.
"""

from __future__ import annotations

import pytest
from aegis.services.integrations_config import (
    CONFIG_REGISTRY,
    apply_config_overrides,
    save_integration,
)
from aegis.services.slack_config import resolve_slack_config, slack_config_status


class _S:
    """Minimal Settings stand-in carrying the fields the resolver reads."""

    secret_key = "test-secret-key"
    slack_owner_member_id = ""
    slack_saveit_emoji = "brain"
    slack_note_to_self_channel = ""


def test_registry_declares_both_self_capture_keys():
    keys = {c.key for c in CONFIG_REGISTRY}
    assert "slack_saveit_emoji" in keys
    assert "slack_note_to_self_channel" in keys
    assert "slack_owner_member_id" in keys  # the identity filter B2 depends on


def test_registry_keys_exist_on_settings():
    """A ConfigKey whose name isn't a Settings field is silently unreachable —
    `apply_config_overrides` setattr's it but nothing ever reads it."""
    from aegis.config import Settings

    for key in ("slack_saveit_emoji", "slack_note_to_self_channel"):
        assert key in Settings.model_fields, f"{key} missing from Settings"


async def test_internal_slack_config_carries_self_capture_knobs(db_pool):
    """The values comms needs are present in the internal resolver output."""
    settings = _S()
    settings.slack_owner_member_id = "UOWNER"
    settings.slack_saveit_emoji = "brain,memo"
    settings.slack_note_to_self_channel = "CNOTES"

    resolved = await resolve_slack_config(db_pool, settings)

    assert resolved["owner_member_id"] == "UOWNER"
    assert resolved["saveit_emoji"] == "brain,memo"
    assert resolved["note_to_self_channel"] == "CNOTES"


async def test_admin_saved_value_reaches_the_internal_resolver(db_pool):
    """End-to-end for the config path: admin UI save → settings row → boot
    overlay → the payload comms fetches."""
    settings = _S()
    await save_integration(db_pool, settings, "slack_note_to_self_channel", "CFROMDB")
    await save_integration(db_pool, settings, "slack_saveit_emoji", "bulb")
    try:
        await apply_config_overrides(settings, db_pool)
        assert settings.slack_note_to_self_channel == "CFROMDB"

        resolved = await resolve_slack_config(db_pool, settings)
        assert resolved["note_to_self_channel"] == "CFROMDB"
        assert resolved["saveit_emoji"] == "bulb"
    finally:
        await db_pool.execute(
            "DELETE FROM settings WHERE key IN "
            "('integration:slack_note_to_self_channel', 'integration:slack_saveit_emoji')"
        )


async def test_admin_status_endpoint_still_leaks_no_secrets(db_pool):
    """`slack_config_status` feeds the browser — it must keep returning only
    booleans/channel even though the resolver now returns more fields."""
    status = await slack_config_status(db_pool, _S())
    assert set(status) == {
        "bot_token_set", "app_token_set", "channel", "configured", "source"
    }


@pytest.mark.parametrize(
    "key", ["slack_saveit_emoji", "slack_note_to_self_channel"]
)
async def test_new_keys_are_saveable_through_the_admin_path(db_pool, key):
    """`save_integration` rejects unknown keys, so this proves the admin UI can
    actually store them."""
    settings = _S()
    await save_integration(db_pool, settings, key, "xyz")
    try:
        row = await db_pool.fetchrow(
            "SELECT value FROM settings WHERE key = $1", f"integration:{key}"
        )
        assert row is not None and row["value"]["val"] == "xyz"
    finally:
        await db_pool.execute(
            "DELETE FROM settings WHERE key = $1", f"integration:{key}"
        )
