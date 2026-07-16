"""Every /api route must sit behind verify_auth — regression guard for #88.

The issue asked for "an audit of which routes skip Depends(verify_auth)". This
makes that audit permanent: it walks every route the app actually registers and
asserts an anonymous request gets 401. A new router that forgets
``dependencies=[Depends(verify_auth)]`` fails this test.

Allowlisted by design:
  * ``/health``          — liveness probe, must answer without credentials.
  * ``/api/webhooks/*``  — each verifies its own HMAC (github/sentry/todoist) or
                           the optional X-Alert-Token shared secret (alert).

Router-level dependencies run before endpoint parameter validation, so filling
path params with dummy values can never turn a 401 into a 422 — an unauthed
route is the only way to get a non-401 here.
"""

from __future__ import annotations

import re

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

_TEST_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "n8n_ui_url": "https://n8n.example.com",
    "n8n_webhook_secret": "test-secret",
    # Pinned explicitly so a developer's config/.env can never relax the guard.
    "auth_disabled": False,
    "admin_username": "admin",
    "admin_password": "route-guard-test",
    "api_key": "",
}

# Paths legitimately reachable without verify_auth.
_ALLOWLIST_EXACT = {"/health"}
_ALLOWLIST_PREFIXES = ("/api/webhooks/",)


@pytest.fixture
def settings() -> Settings:
    return Settings(**_TEST_SETTINGS)


def _walk(routes) -> list[APIRoute]:
    """Flatten every APIRoute reachable from an app/router route list.

    FastAPI 0.139 wraps each ``include_router`` call in a private
    ``_IncludedRouter`` holding the real router under ``original_router``,
    rather than flattening APIRoutes onto ``app.routes`` the way older versions
    did. Handle both shapes so a FastAPI upgrade can't silently empty this walk
    (the >50 assertion below is the backstop if it ever does).
    """
    found: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
            continue
        nested = getattr(route, "original_router", None) or route
        sub = getattr(nested, "routes", None)
        if sub and nested is not route:
            found.extend(_walk(sub))
    return found


def _api_routes(app) -> list[tuple[str, str]]:
    """Every registered /api route as (method, path), minus the allowlist."""
    out: list[tuple[str, str]] = []
    for route in _walk(app.routes):
        path = route.path
        if not path.startswith("/api"):
            continue  # /health, /openapi.json, SPA fallback
        if path in _ALLOWLIST_EXACT or path.startswith(_ALLOWLIST_PREFIXES):
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            out.append((method, path))
    return out


@pytest_asyncio.fixture(loop_scope="function")
async def auth_app(settings):
    app = create_app(run_lifespan=False)
    app.dependency_overrides[get_settings] = lambda: settings
    return app


async def test_every_api_route_rejects_anonymous(auth_app):
    routes = _api_routes(auth_app)
    # Sanity-check the walk itself: if create_app ever stops registering
    # routers, an empty list would make this test vacuously green.
    assert len(routes) > 50, f"route walk looks broken, found only: {routes}"

    failures = []
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for method, path in routes:
            concrete = re.sub(r"\{[^}]+\}", "x", path)
            resp = await client.request(method, concrete)
            if resp.status_code != 401:
                failures.append((method, path, resp.status_code))

    assert not failures, f"routes reachable without auth: {failures}"


async def test_health_is_reachable_anonymously(auth_app):
    """The liveness probe must stay open — it is the one deliberate exception."""
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200


async def test_auth_disabled_allows_anonymous(settings):
    """auth_disabled=true must keep working — Cloudflare-Access-fronted
    deployments legitimately rely on it (config.py documents this)."""
    settings.auth_disabled = True
    app = create_app(run_lifespan=False)
    app.dependency_overrides[get_settings] = lambda: settings
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # /api/agents/meta/options is a static vocab dump — no db_pool needed,
        # so a non-401 here proves auth was bypassed rather than the handler
        # erroring before we can tell.
        resp = await client.get("/api/agents/meta/options")
    assert resp.status_code != 401
