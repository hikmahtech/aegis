"""BYO integration config — registry, encrypted secrets, boot overlay."""

from __future__ import annotations

import types

import pytest
import pytest_asyncio
from aegis.services.integrations_config import (
    CONFIG_REGISTRY,
    apply_config_overrides,
    get_integrations,
    save_integration,
)


def _settings(secret_key: str = "", **env):
    ns = types.SimpleNamespace(secret_key=secret_key)
    for c in CONFIG_REGISTRY:
        setattr(ns, c.key, "")
    for k, v in env.items():
        setattr(ns, k, v)
    return ns


@pytest_asyncio.fixture(loop_scope="function")
async def clean_int(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key LIKE 'integration:%'")
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key LIKE 'integration:%'")


async def test_save_and_overlay_secret(clean_int):
    await save_integration(clean_int, _settings(secret_key="k"), "github_token", "gh_x")
    s2 = _settings(secret_key="k")  # env blank
    await apply_config_overrides(s2, clean_int)
    assert s2.github_token == "gh_x"  # overlaid from DB (decrypted)


async def test_env_fallback_when_no_db(clean_int):
    s = _settings(github_token="env-token")
    await apply_config_overrides(s, clean_int)  # no DB rows → unchanged
    assert s.github_token == "env-token"


async def test_get_integrations_secret_never_returns_value(clean_int):
    s = _settings(secret_key="k")
    await save_integration(clean_int, s, "github_token", "sk-secret")
    items = await get_integrations(clean_int, s)
    tok = next(i for i in items if i["key"] == "github_token")
    assert tok["secret"] and tok["set"] and tok["value"] is None and tok["source"] == "db"


async def test_non_secret_value_shown(clean_int):
    s = _settings()
    await save_integration(clean_int, s, "searxng_url", "http://searx.example")
    items = await get_integrations(clean_int, s)
    org = next(i for i in items if i["key"] == "searxng_url")
    assert org["value"] == "http://searx.example" and not org["secret"] and org["source"] == "db"


async def test_unknown_key_raises(clean_int):
    with pytest.raises(ValueError):
        await save_integration(clean_int, _settings(), "not_a_key", "x")


async def test_boolean_flag_overlay_coerces_and_overrides_env(clean_int):
    # env says enabled, DB says "false" → overlay must yield a real bool False.
    s = _settings(money_hygiene_enabled=True)
    await save_integration(clean_int, s, "money_hygiene_enabled", "false")
    await apply_config_overrides(s, clean_int)
    assert s.money_hygiene_enabled is False
    # flip on
    await save_integration(clean_int, s, "money_hygiene_enabled", "true")
    await apply_config_overrides(s, clean_int)
    assert s.money_hygiene_enabled is True


async def test_boolean_flag_get_state(clean_int):
    s = _settings()
    await save_integration(clean_int, s, "tts_enabled", "true")
    items = await get_integrations(clean_int, s)
    t = next(i for i in items if i["key"] == "tts_enabled")
    assert t["boolean"] is True and t["value"] is True and t["source"] == "db"


async def test_a_db_value_overrides_the_env(clean_int):
    """A registry key's DB value overrides the env default."""
    s = _settings(finance_indices="^GSPC")
    await save_integration(clean_int, s, "finance_indices", "^NSEI")
    await apply_config_overrides(s, clean_int)
    assert s.finance_indices == "^NSEI"


async def test_owner_emails_overlay_from_db(clean_int):
    """owner_emails is registry-backed: settable from the admin UI with no
    redeploy, and a blank DB row keeps whatever the env had."""
    s = _settings(owner_emails="")
    await save_integration(clean_int, s, "owner_emails", "me@hikmah.com, me@work.io")
    await apply_config_overrides(s, clean_int)
    assert s.owner_emails == "me@hikmah.com, me@work.io"


async def test_an_empty_db_value_keeps_the_env(clean_int):
    s = _settings(finance_indices="^GSPC")
    await save_integration(clean_int, s, "finance_indices", "")
    await apply_config_overrides(s, clean_int)
    assert s.finance_indices == "^GSPC"


def test_the_infra_lane_keys_left_the_registry():
    """The infra lane moved to the DevOps vertical (a2-devops); its keys are not
    offered on the Integrations page any more."""
    keys = {c.key for c in CONFIG_REGISTRY}
    for gone in (
        "homelab_enabled", "infra_cluster", "infra_heartbeat_ping_url",
        "vercel_token", "vercel_team_id",
    ):
        assert gone not in keys


def test_the_coding_and_registry_keys_left_settings_and_the_registry():
    """The coding lane and the infra registry moved out of v1 (the Development
    vertical, a2-development); their Settings fields and the registry's
    running-services key went with them."""
    from aegis.config import Settings

    keys = {c.key for c in CONFIG_REGISTRY}
    for gone in (
        "remote_script_host", "remote_script_user", "remote_script_key_file",
        "remote_script_known_hosts", "remote_script_repo_base", "remote_script_kimi_host",
        "remote_script_tmux_session", "remote_script_tmux_window_cap",
        "remote_script_claude_orgs", "kimi_cli_binary_path", "claude_cli_binary_path",
        "claude_personal_config_dir", "aegis_self_repo_path", "mcp_server_enabled",
        "mcp_server_allow_unauthenticated", "mcp_server_external_url",
        "mcp_gate_wait_seconds", "aegis_stack_name",
    ):
        assert gone not in Settings.model_fields, gone
        assert gone not in keys, gone


def test_every_config_key_is_a_settings_field():
    """Boot sets each stored config row onto Settings; a key with no field crashed core and
    worker at startup once the row was stored (#703's money_fanout_enabled, 2026-10-07)."""
    from aegis.config import Settings

    missing = [c.key for c in CONFIG_REGISTRY if c.key not in Settings.model_fields]
    assert missing == []

