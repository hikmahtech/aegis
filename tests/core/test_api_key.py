"""Admin-generated API key — generate endpoint, status endpoint, verify_auth.

DB-first with env fallback:
- POST /api/admin/api-key/generate mints a key server-side, stores it
  encrypted in the settings table, and returns the cleartext exactly once.
- GET /api/admin/api-key exposes only {configured, source} — never the key.
- verify_auth accepts X-API-Key matching EITHER the DB-stored key (short TTL
  cache, invalidated on generate) OR the env AEGIS_API_KEY fallback.

Route tests use the standard override pattern
(``app.dependency_overrides[get_settings]``) against a real Postgres pool
(:25432) — no DB mocks.
"""

from __future__ import annotations

from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.services.api_key import (
    SETTINGS_KEY,
    api_key_status,
    generate_api_key,
    invalidate_api_key_cache,
    resolve_api_key,
)
from httpx import ASGITransport, AsyncClient

_BASE = {
    "database_url": "postgresql://test:test@localhost/test",
    "litellm_url": "https://litellm.test/v1",
    "admin_username": "admin",
    "admin_password": "admin-pass",
    "secret_key": "test-secret-key-for-api-key-tests",
}

BASIC_AUTH = ("admin", "admin-pass")


def _make_settings(**overrides) -> Settings:
    return Settings(**{**_BASE, **overrides})


def _make_app(settings: Settings, db_pool):
    app = create_app(run_lifespan=False)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.db_pool = db_pool
    app.state.settings = settings
    return app


async def _reset(db_pool) -> None:
    """Start from no stored key and a cold cache (module-level TTL cache)."""
    invalidate_api_key_cache()
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM settings WHERE key = $1", SETTINGS_KEY)


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------


async def test_generate_stores_encrypted_and_resolves(db_pool):
    await _reset(db_pool)
    settings = _make_settings()
    key = await generate_api_key(db_pool, settings)
    assert isinstance(key, str) and len(key) >= 32

    # Stored value must NOT contain the cleartext (encrypted with secret_key).
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY)
    assert row is not None
    assert key not in str(row["value"])

    # But it resolves back to the cleartext server-side.
    assert await resolve_api_key(db_pool, settings, use_cache=False) == key


async def test_generate_overwrites_previous_key(db_pool):
    await _reset(db_pool)
    settings = _make_settings()
    first = await generate_api_key(db_pool, settings)
    second = await generate_api_key(db_pool, settings)
    assert first != second
    # Cache was invalidated on generate — the fresh key resolves immediately.
    assert await resolve_api_key(db_pool, settings) == second


async def test_status_reports_db_env_none(db_pool):
    await _reset(db_pool)
    settings = _make_settings()
    assert await api_key_status(db_pool, settings) == {"configured": False, "source": "none"}

    env_settings = _make_settings(api_key="env-fallback-key")
    status = await api_key_status(db_pool, env_settings)
    assert status == {"configured": True, "source": "env"}

    await generate_api_key(db_pool, settings)
    status = await api_key_status(db_pool, settings)
    assert status == {"configured": True, "source": "db"}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def test_generate_endpoint_returns_key_once_and_status_never_does(db_pool):
    await _reset(db_pool)
    settings = _make_settings()
    app = _make_app(settings, db_pool)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/admin/api-key/generate", auth=BASIC_AUTH)
        assert resp.status_code == 200
        body = resp.json()
        key = body["api_key"]
        assert body["configured"] is True
        assert isinstance(key, str) and len(key) >= 32

        # Status endpoint: booleans only, never the key.
        resp = await client.get("/api/admin/api-key", auth=BASIC_AUTH)
        assert resp.status_code == 200
        assert resp.json() == {"configured": True, "source": "db"}
        assert key not in resp.text


async def test_generate_endpoint_requires_auth(db_pool):
    await _reset(db_pool)
    settings = _make_settings()
    app = _make_app(settings, db_pool)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/admin/api-key/generate")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# verify_auth: DB key + env fallback
# ---------------------------------------------------------------------------


async def test_verify_auth_accepts_db_generated_key(db_pool):
    await _reset(db_pool)
    settings = _make_settings()
    app = _make_app(settings, db_pool)
    key = await generate_api_key(db_pool, settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # No basic auth — X-API-Key alone must authenticate.
        resp = await client.get("/api/admin/api-key", headers={"X-API-Key": key})
        assert resp.status_code == 200

        resp = await client.get("/api/admin/api-key", headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 401


async def test_verify_auth_env_fallback_still_works(db_pool):
    await _reset(db_pool)
    settings = _make_settings(api_key="env-fallback-key")
    app = _make_app(settings, db_pool)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/admin/api-key", headers={"X-API-Key": "env-fallback-key"})
        assert resp.status_code == 200


async def test_verify_auth_db_key_works_alongside_env_key(db_pool):
    """Both credentials are accepted — env for legacy callers (comms/worker),
    DB for admin-generated keys."""
    await _reset(db_pool)
    settings = _make_settings(api_key="env-fallback-key")
    app = _make_app(settings, db_pool)
    db_key = await generate_api_key(db_pool, settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for candidate in ("env-fallback-key", db_key):
            resp = await client.get("/api/admin/api-key", headers={"X-API-Key": candidate})
            assert resp.status_code == 200, candidate


async def test_verify_auth_old_db_key_rejected_after_regenerate(db_pool):
    await _reset(db_pool)
    settings = _make_settings()
    app = _make_app(settings, db_pool)
    old_key = await generate_api_key(db_pool, settings)
    new_key = await generate_api_key(db_pool, settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/admin/api-key", headers={"X-API-Key": new_key})
        assert resp.status_code == 200
        resp = await client.get("/api/admin/api-key", headers={"X-API-Key": old_key})
        assert resp.status_code == 401


async def test_generic_settings_editor_hides_api_key_row(db_pool):
    """The raw /api/settings editor must never expose the stored key row."""
    await _reset(db_pool)
    settings = _make_settings()
    app = _make_app(settings, db_pool)
    await generate_api_key(db_pool, settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/settings", auth=BASIC_AUTH)
        assert resp.status_code == 200
        assert all(r["key"] != SETTINGS_KEY for r in resp.json())
        resp = await client.get(f"/api/settings/{SETTINGS_KEY}", auth=BASIC_AUTH)
        assert resp.status_code == 403
