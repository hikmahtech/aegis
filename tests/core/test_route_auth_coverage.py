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

# Paths legitimately reachable without verify_auth. Every entry is a deliberate
# decision, not an accident of where the path sits — see _guarded_routes.
_ALLOWLIST_EXACT = {
    "/health",  # liveness probe; must answer without credentials
    # The admin-panel SPA shell. Serving the bundle anonymously is intentional
    # and unavoidable — a browser has to fetch the login page before it can
    # authenticate. The API calls the shell then makes are all guarded.
    "/{path:path}",
}
_ALLOWLIST_PREFIXES = (
    "/api/webhooks/",  # each verifies its own HMAC / shared secret
    "/assets",  # hashed JS/CSS for the SPA shell, StaticFiles mount
)

# Routes that must NOT exist at all under default settings. Allowlisting these
# would be wrong: they aren't "public by design", they're FastAPI defaults that
# leak a full API map, so `expose_api_docs` is off unless an operator opts in
# (#305). Asserted absent rather than asserted-401, because gating them behind
# auth is no protection in an `auth_disabled=true` deployment.
_MUST_NOT_EXIST = ("/docs", "/redoc", "/openapi.json")


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


def _guarded_routes(app) -> list[tuple[str, str]]:
    """Every registered route as (method, path), minus the public allowlist.

    Walks **all** routes, not just ``/api`` ones (#306). The previous version
    skipped anything outside ``/api``, which meant a route mounted elsewhere was
    invisible to this audit and shipped unauthenticated with no CI signal — which
    is exactly how FastAPI's own ``/docs`` and ``/openapi.json`` stayed anonymous
    (#305) while this test reported full coverage.

    Being public is now an explicit allowlist entry rather than a side effect of
    where a path happens to sit, so a new anonymous route has to be added to
    ``_ALLOWLIST_*`` in a diff a human reads.
    """
    out: list[tuple[str, str]] = []
    for route in _walk(app.routes):
        path = route.path
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


async def test_every_route_rejects_anonymous(auth_app):
    routes = _guarded_routes(auth_app)
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


async def test_audit_catches_unauthenticated_routes_outside_api(auth_app):
    """Proves the widened walk has teeth (#306).

    Necessary because of a nasty property of this suite: in a test environment
    the ONLY non-``/api`` route registered is ``/health`` (the SPA catch-all
    needs a built ``dist/``, and the docs routes are off by default), so
    switching the walk from "/api only" to "everything" cannot be observed by
    the other tests here — they would pass identically either way.

    So inject a route where the old filter had a blind spot and assert the audit
    both lists it and flags it as anonymous. Revert the walk to
    ``path.startswith("/api")`` and this fails.
    """
    auth_app.add_api_route("/sneaky-unauthed", lambda: {"ok": True}, methods=["GET"])

    assert ("GET", "/sneaky-unauthed") in _guarded_routes(auth_app), (
        "audit is blind to routes outside /api — the #306 regression"
    )

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/sneaky-unauthed")
    assert resp.status_code == 200, "sanity: the injected route really is anonymous"


async def test_interactive_docs_are_off_by_default(auth_app):
    """#305: /docs, /redoc and /openapi.json must not be registered at all.

    FastAPI adds these itself, outside the router tree every /api path goes
    through, so they carry no auth dependency. An anonymous caller reading
    /openapi.json gets every endpoint, parameter and schema in one request.
    """
    # getattr: app.routes also holds _IncludedRouter wrappers with no `.path`.
    registered = {p for r in auth_app.routes if (p := getattr(r, "path", None))}
    exposed = [p for p in _MUST_NOT_EXIST if p in registered]
    assert not exposed, f"interactive docs exposed by default: {exposed}"

    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for path in _MUST_NOT_EXIST:
            resp = await client.get(path)
            # The SPA catch-all answers unknown paths with index.html, so 200 is
            # only acceptable when it is NOT the schema. A JSON body here would
            # mean openapi is still being served.
            assert resp.status_code != 200 or "openapi" not in resp.text[:200], (
                f"{path} still serving API schema anonymously"
            )


async def test_interactive_docs_can_be_opted_into(settings):
    """The switch has to actually work, or developers will just delete it."""
    settings.expose_api_docs = True
    app = create_app(run_lifespan=False, settings=settings)
    registered = {p for r in app.routes if (p := getattr(r, "path", None))}
    assert {"/docs", "/openapi.json"} <= registered, (
        f"expose_api_docs=True did not register the docs routes: {sorted(registered)[:10]}"
    )


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
