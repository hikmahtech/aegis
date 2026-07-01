"""Gmail reauth OAuth routes."""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from fastapi.testclient import TestClient


def _make_flow_mock():
    """Build a fake google_auth_oauthlib.flow module + Flow class.

    google_auth_oauthlib is not installed in the test venv, so we inject
    a lightweight fake into sys.modules so the lazy `from ... import Flow`
    inside the route handlers resolves to our mock.
    """
    fake_module = types.ModuleType("google_auth_oauthlib")
    fake_flow_module = types.ModuleType("google_auth_oauthlib.flow")

    class FakeFlow:
        _instance: FakeFlow | None = None

        def __init__(self):
            self.credentials = MagicMock()
            self.credentials.to_json = MagicMock(return_value='{"token": "mock-token"}')
            self._auth_url = "https://accounts.google.com/o/oauth2/auth?response_type=code"
            self.authorization_url = MagicMock(return_value=(self._auth_url, "state-tok"))
            self.fetch_token = MagicMock()
            self.code_verifier = None
            FakeFlow._instance = self

        @classmethod
        def from_client_secrets_file(cls, *args, **kwargs):
            return cls()

        @classmethod
        def from_client_config(cls, *args, **kwargs):
            return cls()

    fake_flow_module.Flow = FakeFlow
    fake_module.flow = fake_flow_module
    sys.modules["google_auth_oauthlib"] = fake_module
    sys.modules["google_auth_oauthlib.flow"] = fake_flow_module
    return FakeFlow


# Install the fake at module load time so all tests share it.
FakeFlow = _make_flow_mock()

_TEST_REQUIRED_SETTINGS: dict = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "n8n_ui_url": "https://n8n.example.com",
    "admin_username": "a",
    "admin_password": "p",
    "api_key": "k",
    "n8n_webhook_secret": "test-secret",
}


@pytest.fixture
def settings(tmp_path):
    creds_file = tmp_path / "creds.json"
    creds_file.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "cid",
                    "client_secret": "cs",
                    "redirect_uris": ["http://localhost/cb"],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        )
    )
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    return Settings(
        **_TEST_REQUIRED_SETTINGS,
        gmail_credentials_file=str(creds_file),
        gmail_token_dir=str(token_dir),
        aegis_ui_url="https://aegis.example.com",
    )


@pytest.fixture
def app_and_client(settings):
    app = create_app(run_lifespan=False)
    # The reauth flow reads the OAuth client DB-first; return None so it falls
    # back to the credentials file the `settings` fixture provides.
    app.state.db_pool = AsyncMock()
    app.state.db_pool.fetchrow = AsyncMock(return_value=None)
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app, follow_redirects=False)
    return app, client


def test_initiate_redirects_to_google(app_and_client):
    _, client = app_and_client
    resp = client.get(
        "/api/admin/gmail/reauth/sebas/initiate",
        headers={"X-API-Key": "k"},
    )
    assert resp.status_code == 302
    loc = resp.headers.get("location", "")
    assert loc.startswith("https://accounts.google.com/o/oauth2/auth")


def test_initiate_passes_label_in_state(app_and_client):
    _, client = app_and_client

    captured_state: list[str] = []

    def capturing_from_file(*args, **kwargs):
        flow = FakeFlow()
        orig_auth_url = flow.authorization_url

        def capturing_auth_url(**kw):
            captured_state.append(kw.get("state", ""))
            return orig_auth_url(**kw)

        flow.authorization_url = capturing_auth_url
        return flow

    with patch.object(FakeFlow, "from_client_config", side_effect=capturing_from_file):
        resp = client.get(
            "/api/admin/gmail/reauth/sebas/initiate?interaction_id=ia-99",
            headers={"X-API-Key": "k"},
        )

    assert resp.status_code == 302
    assert captured_state, "authorization_url was not called"
    assert "sebas" in captured_state[0]
    assert "ia-99" in captured_state[0]


def test_initiate_without_oauth_client_returns_503(tmp_path):
    s = Settings(
        **_TEST_REQUIRED_SETTINGS,
        gmail_credentials_file="",
        gmail_token_dir=str(tmp_path),
    )
    app = create_app(run_lifespan=False)
    app.state.db_pool = AsyncMock()
    app.state.db_pool.fetchrow = AsyncMock(return_value=None)  # no DB client either
    app.dependency_overrides[get_settings] = lambda: s
    client = TestClient(app, follow_redirects=False)

    resp = client.get(
        "/api/admin/gmail/reauth/sebas/initiate",
        headers={"X-API-Key": "k"},
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "google_oauth_client_not_configured"


def test_callback_saves_token_and_returns_ok(app_and_client, settings):
    _, client = app_and_client

    # Override to_json on the class-level mock credentials
    FakeFlow._instance = None  # reset

    with patch("aegis.api.routes.gmail_reauth.httpx.AsyncClient") as MockHttp:  # noqa: N806
        mock_http_instance = AsyncMock()
        mock_http_instance.__aenter__ = AsyncMock(return_value=mock_http_instance)
        mock_http_instance.__aexit__ = AsyncMock(return_value=False)
        MockHttp.return_value = mock_http_instance

        resp = client.get(
            "/api/admin/gmail/reauth/sebas/callback?code=test-code&state=sebas:",
            headers={"X-API-Key": "k"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["label"] == "sebas"
    assert body["interaction_resolved"] is None

    import os

    saved = os.path.join(settings.gmail_token_dir, "sebas.json")
    assert os.path.exists(saved)


def test_callback_with_interaction_id_posts_resolve(app_and_client):
    _, client = app_and_client

    posted_to: list[str] = []

    async def fake_post(url, **kwargs):
        posted_to.append(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        return mock_resp

    with patch("aegis.api.routes.gmail_reauth.httpx.AsyncClient") as MockHttp:  # noqa: N806
        mock_http_instance = AsyncMock()
        mock_http_instance.__aenter__ = AsyncMock(return_value=mock_http_instance)
        mock_http_instance.__aexit__ = AsyncMock(return_value=False)
        mock_http_instance.post = AsyncMock(side_effect=fake_post)
        MockHttp.return_value = mock_http_instance

        resp = client.get(
            "/api/admin/gmail/reauth/sebas/callback?code=xyz&state=sebas:ia-ia-1",
            headers={"X-API-Key": "k"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["interaction_resolved"] == "ia-ia-1"
    # Verify POST was fired to the resolve endpoint
    assert len(posted_to) == 1
    assert "/api/interactions/ia-ia-1/resolve" in posted_to[0]
