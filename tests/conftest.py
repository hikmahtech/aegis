"""Shared test fixtures for AEGIS v2."""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis.config import Settings


def pytest_sessionstart(session: pytest.Session) -> None:
    """Guard against aegis#96: wrong-checkout tests running silently green (or red).

    This repo's `.venv` is built from editable installs
    (`pip install -e core -e worker -e comms`), so the interpreter's
    site-packages `.pth` entries point at wherever that `pip install` was run
    from — normally the main checkout. A bare `pytest` invoked from a git
    worktree still finds those `.pth` entries first and imports `aegis` (and
    `aegis_worker` / `aegis_comms`) from the MAIN checkout's `core/src/`, not
    from the worktree whose tests are being collected. The suite then
    exercises unmodified main code while running the worktree's test files —
    a false green for the change under test, or a confusing false red.

    Detect it here rather than let it happen quietly: the already-imported
    `aegis` package must resolve to a path under this session's rootdir.
    """
    import aegis

    pkg_file = getattr(aegis, "__file__", None)
    if not pkg_file:
        return  # namespace package with no __file__: nothing to check
    pkg_path = Path(pkg_file).resolve()
    rootdir = Path(str(session.config.rootdir)).resolve()

    if rootdir != pkg_path and rootdir not in pkg_path.parents:
        pytest.exit(
            f"aegis#96 guard: `aegis` package imported from {pkg_path}, which "
            f"is outside the pytest rootdir {rootdir}. This checkout's editable "
            "install resolves to a DIFFERENT clone (likely the main checkout) — "
            "the suite would silently test that code, not this one.\n"
            "Fix: PYTHONPATH=core/src:worker/src:comms/src pytest ...",
            returncode=1,
        )

# Postgres server from `docker compose up -d postgres`. Tests get their own
# database on it (below) — never the long-lived `aegis` dev database.
_PG_SERVER = "postgresql://aegis:aegis_dev@localhost:25432"
_TEST_DB = "aegis_test"
_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def test_db_url() -> str | None:
    """URL of a freshly-created, freshly-migrated + seeded session-scoped
    test database.

    `TEST_DATABASE_URL` overrides everything (caller-managed: no drop/create,
    no migrate). Otherwise `aegis_test` is dropped, recreated, migrated from
    this checkout's migrations/, and seeded from config/seed/ (same as core
    boot) once per session — sharing the dev `aegis` database broke the suite
    whenever a parallel branch applied a divergent migration to it (e.g. the
    maou→finance schema rename).

    Returns None when no Postgres is reachable; db_pool fixtures then skip.
    """
    override = os.getenv("TEST_DATABASE_URL")
    if override:
        return override

    async def _prepare() -> str:
        import asyncpg
        from aegis.db import create_pool, run_migrations
        from aegis.seed import load_seeds

        admin = await asyncpg.connect(f"{_PG_SERVER}/aegis")
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {_TEST_DB} WITH (FORCE)")
            await admin.execute(f"CREATE DATABASE {_TEST_DB}")
        finally:
            await admin.close()
        url = f"{_PG_SERVER}/{_TEST_DB}"
        pool = await create_pool(url, min_size=1, max_size=2)
        try:
            await run_migrations(pool, _REPO_ROOT / "migrations")
            await load_seeds(pool, _REPO_ROOT / "config" / "seed")
        finally:
            await pool.close()
        return url

    try:
        return asyncio.run(_prepare())
    except OSError:
        return None

# Defaults for Settings fields that are now REQUIRED (no production default)
# but still need a value to instantiate the model in tests.
_TEST_REQUIRED_SETTINGS: dict = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "n8n_ui_url": "https://n8n.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
    "n8n_webhook_secret": "test-secret",
}


@pytest.fixture
def test_settings() -> Settings:
    """Settings with test-safe defaults."""
    return Settings(**_TEST_REQUIRED_SETTINGS)


def _make_pool_acquire(fetchval_return=None):
    """Return a MagicMock for pool.acquire() that supports `async with pool.acquire() as conn`.

    tier.resolve_model_for_agent uses this pattern. By default fetchval returns None,
    which makes the tier resolver fall back to 'balanced'.
    """
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=fetchval_return)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


@pytest.fixture
def mock_db_pool():
    """Mock asyncpg pool."""
    pool = AsyncMock()
    pool.fetchval.return_value = 1
    pool.fetch.return_value = []
    pool.fetchrow.return_value = None
    pool.execute.return_value = "OK"
    pool.close = AsyncMock()
    # Support `async with pool.acquire() as conn:` used by resolve_model_for_agent.
    # Returns None from fetchval → tier resolver falls back to 'balanced'.
    pool.acquire = _make_pool_acquire(fetchval_return=None)
    return pool


@pytest.fixture(autouse=True, scope="session")
def _load_model_tiers_for_tests() -> None:
    """Ensure the tier map is populated for all tests.

    Tests that use send_message (or resolve_model_for_agent) need _TIERS populated
    or the 'balanced' fallback will KeyError. This fixture sets a minimal map once
    per session so all tests start with a working tier resolver.
    """
    from aegis.llm.tier import set_model_tiers

    set_model_tiers({"fast": "gemma4:e2b", "balanced": "qwen3:14b", "smart": "qwen3:32b"})
