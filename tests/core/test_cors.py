"""Tests for CORS middleware configuration (D3).

Before the fix, `create_app()` added `CORSMiddleware` with
`allow_origins=["*"]`, `allow_credentials=True`, `allow_methods=["*"]`,
`allow_headers=["*"]`. Per the CORS spec, browsers refuse
`Access-Control-Allow-Origin: *` together with credentialed requests, so
that combination was dead/misleading config in production and needlessly
permissive (wildcard methods/headers) if it ever did take effect (e.g.
behind a proxy that rewrites the Origin header).

This is a single-origin self-hosted deployment: the admin-panel SPA is
served from the same origin as the API, so the admin panel itself never
triggers a CORS check at all (same-origin requests aren't subject to CORS).
These tests only assert the middleware *configuration* AEGIS builds, not
admin-panel runtime behavior.
"""

from aegis.api.app import create_app
from aegis.config import Settings
from starlette.middleware.cors import CORSMiddleware


def _cors_kwargs(app):
    """Return the kwargs `create_app` passed to `CORSMiddleware`."""
    for mw in app.user_middleware:
        if mw.cls is CORSMiddleware:
            return mw.kwargs
    raise AssertionError("CORSMiddleware is not registered on the app")


def _assert_never_wildcard_with_credentials(kwargs: dict) -> None:
    """The invariant this fix must hold under ANY configuration: browsers
    reject `Access-Control-Allow-Origin: *` combined with
    `Access-Control-Allow-Credentials: true` outright, so AEGIS must never
    produce that pair regardless of how `cors_allowed_origins` is set."""
    if kwargs.get("allow_credentials"):
        assert kwargs.get("allow_origins") != ["*"], (
            "allow_origins=['*'] with allow_credentials=True is invalid per "
            "the CORS spec — browsers silently refuse it, making this "
            "config dead for credentialed cross-origin calls"
        )


def test_default_empty_origin_list_disables_credentialed_cors(test_settings):
    """The self-hosted single-origin default (cors_allowed_origins=[]):
    no cross-origin access is granted, and the wildcard/credentials
    invariant holds trivially since allow_credentials is off."""
    app = create_app(run_lifespan=False, settings=test_settings)
    kwargs = _cors_kwargs(app)

    assert test_settings.cors_allowed_origins == []
    assert kwargs["allow_origins"] == []
    assert kwargs["allow_credentials"] is False
    _assert_never_wildcard_with_credentials(kwargs)


def test_configured_origin_list_enables_scoped_credentials(test_settings):
    """A configured cors_allowed_origins allowlist turns allow_credentials
    on for exactly those origins — never combined with a wildcard."""
    configured = test_settings.model_copy(
        update={"cors_allowed_origins": ["https://admin.example.com"]}
    )
    app = create_app(run_lifespan=False, settings=configured)
    kwargs = _cors_kwargs(app)

    assert kwargs["allow_origins"] == ["https://admin.example.com"]
    assert kwargs["allow_credentials"] is True
    _assert_never_wildcard_with_credentials(kwargs)


def test_cors_allowed_origins_env_var_parses_comma_separated_list(monkeypatch):
    """AEGIS_CORS_ALLOWED_ORIGINS is a comma-separated env override, parsed
    via the same NoDecode + model_validator(mode="before") convention as
    the existing homelab_public_domains list setting."""
    monkeypatch.setenv("AEGIS_DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("AEGIS_LITELLM_URL", "http://x")
    monkeypatch.setenv("AEGIS_TEMPORAL_UI_URL", "http://x")
    monkeypatch.setenv("AEGIS_ADMIN_USERNAME", "u")
    monkeypatch.setenv("AEGIS_ADMIN_PASSWORD", "p")
    monkeypatch.setenv(
        "AEGIS_CORS_ALLOWED_ORIGINS", "https://a.example.com, https://b.example.com"
    )

    s = Settings()
    assert s.cors_allowed_origins == ["https://a.example.com", "https://b.example.com"]

    app = create_app(run_lifespan=False, settings=s)
    kwargs = _cors_kwargs(app)
    assert kwargs["allow_origins"] == ["https://a.example.com", "https://b.example.com"]
    assert kwargs["allow_credentials"] is True
    _assert_never_wildcard_with_credentials(kwargs)


def test_cors_methods_and_headers_are_scoped_not_wildcard(test_settings):
    """allow_methods/allow_headers must be an explicit, minimal allowlist —
    not ["*"] — matching what admin-panel/frontend/src/api/client.ts
    actually sends: GET/POST/PUT/PATCH/DELETE with Content-Type and
    Authorization headers (grepped from admin-panel/frontend/src/)."""
    app = create_app(run_lifespan=False, settings=test_settings)
    kwargs = _cors_kwargs(app)

    assert kwargs["allow_methods"] != ["*"]
    assert kwargs["allow_headers"] != ["*"]
    assert set(kwargs["allow_methods"]) == {"GET", "POST", "PUT", "PATCH", "DELETE"}
    assert set(kwargs["allow_headers"]) == {"Content-Type", "Authorization"}


def test_bare_create_app_never_produces_wildcard_with_credentials():
    """The exact pre-fix regression: `create_app()` with no settings
    argument at all (production's `aegis/__main__.py: app = create_app()`
    call site, and how most test files build the app) must never produce
    the invalid allow_origins=['*'] + allow_credentials=True combination
    that the old hardcoded CORSMiddleware call always produced."""
    app = create_app(run_lifespan=False)
    kwargs = _cors_kwargs(app)
    _assert_never_wildcard_with_credentials(kwargs)
    assert kwargs["allow_origins"] != ["*"]
