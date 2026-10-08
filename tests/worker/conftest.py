"""Shared fixtures for worker tests (including activities sub-package)."""

from __future__ import annotations

import pytest_asyncio

# A content route the clarify-activities tests route `APP-<n>:` titles with
# (regex mode keeps the required colon). Seeded before each such test, with the
# worker's 30s route cache reset so it re-reads. Gated to the
# `clarify_activities` modules only — resolved lazily via getfixturevalue so it
# never couples the rest of the worker suite to Postgres.
_APP_CONTENT_ROUTE = [
    {
        "key": "jira-app",
        "match": "regex",
        "value": r"^APP-\d+:",
        "assignee": "@raphael",
        "contexts": ["@deep", "@code"],
        "area_label": "@area/acme",
    }
]


@pytest_asyncio.fixture(loop_scope="function")
async def seed_app_route(db_pool):
    """Seed the APP-<n>: content route + reset the worker's 30s route cache
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

