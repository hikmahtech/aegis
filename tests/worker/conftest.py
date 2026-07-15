"""Shared fixtures for worker tests (including activities sub-package)."""

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


# The pre-2026-07 hardcoded Acme `^APP-\d+:` → @pandora investigation is now just
# a content_routes row. The clarify-activities tests exercise that path, so seed
# the equivalent route (regex mode keeps the required colon) before each such test
# and reset the worker's 30s route cache so it re-reads. Gated to the
# `clarify_activities` modules only — resolved lazily via getfixturevalue so it
# never couples the rest of the worker suite to Postgres.
_APP_CONTENT_ROUTE = [
    {
        "key": "jira-app",
        "match": "regex",
        "value": r"^APP-\d+:",
        "gate": True,
        "assignee": "@pandora",
        "contexts": ["@deep", "@code"],
        "area_label": "@area/acme",
        "service": "acme",
        "resource_tags": ["acme"],
    }
]


@pytest_asyncio.fixture(loop_scope="function")
async def seed_app_route(db_pool):
    """Seed the Acme APP-<n>: content route + reset the worker's 30s route cache
    (and clear on teardown). Clarify-activities test modules opt in via a thin
    autouse wrapper, so no other worker test is coupled to this."""
    from aegis.services.content_routes import save_content_routes
    from aegis_worker.activities import clarify as _cl

    _cl._routes_cache.update(routes=None, ts=0.0)
    await save_content_routes(db_pool, _APP_CONTENT_ROUTE)
    _cl._routes_cache.update(routes=None, ts=0.0)
    yield db_pool
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM settings WHERE key='content_routes'")
    _cl._routes_cache.update(routes=None, ts=0.0)
