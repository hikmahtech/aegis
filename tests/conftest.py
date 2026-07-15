"""Shared test fixtures for AEGIS v2."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis.config import Settings

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
