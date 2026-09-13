"""Fixtures shared by all tests/core/ subdirectories.

Child conftests inherit these — db_pool is the real-Postgres fixture
used by both tests/core/test_seed.py and tests/core/db/test_migration_*.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
import yaml
from aegis.db import create_pool
from aegis.services.desk_math import Rules


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


@pytest.fixture
def seeded_desk_config() -> dict:
    """The `trading-desk-daily` config exactly as `config/seed/activities.yaml`
    ships it.

    The desk's code defaults deliberately name no market, no currency and no
    tax law, so the numbers the desk actually produces come from a config row.
    Reading the seed file rather than retyping its values is what makes the
    arithmetic tests evidence that the SHIPPED example still behaves the way it
    always did — retyped constants would agree with themselves for ever while
    the seed drifted.
    """
    path = Path(__file__).resolve().parents[2] / "config" / "seed" / "activities.yaml"
    rows = yaml.safe_load(path.read_text())["activities"]
    return next(r for r in rows if r["slug"] == "trading-desk-daily")["config"]


@pytest.fixture
def seeded_desk_rules(seeded_desk_config) -> Rules:
    """The desk's rules as the seeded example configures them."""
    return Rules.from_config(seeded_desk_config)
