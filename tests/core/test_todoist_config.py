"""BYO Todoist config — API key (DB-first/env, encrypted) + managed projects."""

from __future__ import annotations

import types

import pytest_asyncio
from aegis.services.todoist_config import (
    get_managed_projects,
    resolve_todoist_api_key,
    save_todoist_config,
    todoist_config_status,
)


def _settings(secret_key: str = "", env_key: str = ""):
    return types.SimpleNamespace(secret_key=secret_key, todoist_api_key=env_key)


@pytest_asyncio.fixture(loop_scope="function")
async def clean_tc(db_pool):
    for k in ("todoist_api_key", "todoist_managed_project_ids"):
        await db_pool.execute("DELETE FROM settings WHERE key = $1", k)
    yield db_pool
    for k in ("todoist_api_key", "todoist_managed_project_ids"):
        await db_pool.execute("DELETE FROM settings WHERE key = $1", k)


async def test_env_fallback(clean_tc):
    s = _settings(env_key="envkey")
    assert await resolve_todoist_api_key(clean_tc, s) == "envkey"
    st = await todoist_config_status(clean_tc, s)
    assert st["api_key_set"] and st["source"] == "env"


async def test_db_overrides_env_and_decrypts(clean_tc):
    s = _settings(secret_key="k", env_key="envkey")
    await save_todoist_config(clean_tc, s, api_key="dbkey")
    assert await resolve_todoist_api_key(clean_tc, s) == "dbkey"
    st = await todoist_config_status(clean_tc, s)
    assert st["source"] == "db"


async def test_save_projects_drops_blanks(clean_tc):
    await save_todoist_config(
        clean_tc, _settings(), projects={"inbox": "1", "next": "2", "someday": "  "}
    )
    assert await get_managed_projects(clean_tc) == {"inbox": "1", "next": "2"}


async def test_unconfigured(clean_tc):
    st = await todoist_config_status(clean_tc, _settings())
    assert st["api_key_set"] is False and st["source"] == "none"
