"""Reading one integration value now, DB first (trading desk spec §12)."""

from __future__ import annotations

import pytest_asyncio
from aegis.services.integrations_config import read_integration, save_integration

_KEYS = ("integration:ansaar_url", "integration:ansaar_service_secret")


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key = ANY($1::text[])", list(_KEYS))
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = ANY($1::text[])", list(_KEYS))


async def test_the_db_row_wins_and_a_secret_is_decrypted(pool, test_settings):
    test_settings.ansaar_url = "http://env.example"
    await save_integration(pool, test_settings, "ansaar_url", "http://ansaar-data:3000")
    await save_integration(pool, test_settings, "ansaar_service_secret", "s3cret")
    assert await read_integration(pool, test_settings, "ansaar_url") == "http://ansaar-data:3000"
    assert await read_integration(pool, test_settings, "ansaar_service_secret") == "s3cret"


async def test_no_db_row_falls_back_to_settings_then_empty(pool, test_settings):
    test_settings.ansaar_url = "http://env.example"
    assert await read_integration(pool, test_settings, "ansaar_url") == "http://env.example"
    assert await read_integration(pool, test_settings, "ansaar_service_secret") == ""
