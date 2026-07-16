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
    await save_integration(clean_int, _settings(secret_key="k"), "github_token", "ghp_x")
    s2 = _settings(secret_key="k")  # env blank
    await apply_config_overrides(s2, clean_int)
    assert s2.github_token == "ghp_x"  # overlaid from DB (decrypted)


async def test_env_fallback_when_no_db(clean_int):
    s = _settings(github_token="env-token")
    await apply_config_overrides(s, clean_int)  # no DB rows → unchanged
    assert s.github_token == "env-token"


async def test_get_integrations_secret_never_returns_value(clean_int):
    s = _settings(secret_key="k")
    await save_integration(clean_int, s, "sentry_token", "sk-secret")
    items = await get_integrations(clean_int, s)
    tok = next(i for i in items if i["key"] == "sentry_token")
    assert tok["secret"] and tok["set"] and tok["value"] is None and tok["source"] == "db"


async def test_non_secret_value_shown(clean_int):
    s = _settings()
    await save_integration(clean_int, s, "sentry_org", "myorg")
    items = await get_integrations(clean_int, s)
    org = next(i for i in items if i["key"] == "sentry_org")
    assert org["value"] == "myorg" and not org["secret"] and org["source"] == "db"


async def test_unknown_key_raises(clean_int):
    with pytest.raises(ValueError):
        await save_integration(clean_int, _settings(), "not_a_key", "x")


async def test_boolean_flag_overlay_coerces_and_overrides_env(clean_int):
    # env says enabled, DB says "false" → overlay must yield a real bool False.
    s = _settings(homelab_enabled=True)
    await save_integration(clean_int, s, "homelab_enabled", "false")
    await apply_config_overrides(s, clean_int)
    assert s.homelab_enabled is False
    # flip on
    await save_integration(clean_int, s, "homelab_enabled", "true")
    await apply_config_overrides(s, clean_int)
    assert s.homelab_enabled is True


async def test_boolean_flag_get_state(clean_int):
    s = _settings()
    await save_integration(clean_int, s, "tts_enabled", "true")
    items = await get_integrations(clean_int, s)
    t = next(i for i in items if i["key"] == "tts_enabled")
    assert t["boolean"] is True and t["value"] is True and t["source"] == "db"


async def test_infra_cluster_and_bank_alert_senders_overlay(clean_int):
    """#91 — infra_cluster + bank_alert_senders are now registry-backed:
    a DB value overrides the env default, an unset DB row keeps the env."""
    s = _settings(infra_cluster="env-cluster", bank_alert_senders="")
    await save_integration(clean_int, s, "infra_cluster", "homelab-swarm")
    await save_integration(clean_int, s, "bank_alert_senders", "axisbank.com, hdfcbank.net")
    await apply_config_overrides(s, clean_int)
    assert s.infra_cluster == "homelab-swarm"
    assert s.bank_alert_senders == "axisbank.com, hdfcbank.net"


async def test_infra_cluster_empty_db_value_keeps_env(clean_int):
    s = _settings(infra_cluster="env-cluster")
    await save_integration(clean_int, s, "infra_cluster", "")
    await apply_config_overrides(s, clean_int)
    assert s.infra_cluster == "env-cluster"
