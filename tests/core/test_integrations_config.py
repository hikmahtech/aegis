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
