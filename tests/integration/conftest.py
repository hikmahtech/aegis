"""Fixtures shared by tests/integration/.

Provides a real asyncpg pool for integration tests that need live Postgres.
Mirrors the pattern in tests/core/conftest.py.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from aegis.db import create_pool


@pytest_asyncio.fixture(loop_scope="function")
async def db_pool(test_db_url):
    """Real asyncpg pool on the session's fresh, migrated test database
    (see the root conftest's `test_db_url`).

    Skips the test when no Postgres is reachable (e.g. in CI without a
    postgres service). Set TEST_DATABASE_URL to point at a managed test DB.
    """
    if test_db_url is None:
        pytest.skip("no Postgres reachable for the test database")
    try:
        pool = await create_pool(test_db_url, min_size=1, max_size=5)
    except OSError as exc:
        pytest.skip(f"no Postgres at {test_db_url}: {exc}")
    try:
        yield pool
    finally:
        await pool.close()
