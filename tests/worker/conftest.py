"""Shared fixtures for worker tests (including activities sub-package)."""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from aegis.db import create_pool

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql://aegis:aegis_dev@localhost:25432/aegis",
)


@pytest_asyncio.fixture(loop_scope="function")
async def db_pool():
    """Real asyncpg pool connected to the local dev Postgres instance.

    Skips the test when no Postgres is reachable (e.g. in CI without a
    postgres service). Set TEST_DATABASE_URL to point at a test DB.
    """
    try:
        pool = await create_pool(TEST_DATABASE_URL, min_size=1, max_size=5)
    except OSError as exc:
        pytest.skip(f"no Postgres at {TEST_DATABASE_URL}: {exc}")
    try:
        yield pool
    finally:
        await pool.close()
