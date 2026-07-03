"""AEGIS_AUTH_DISABLED — opt-out of basic auth for authenticating-proxy deployments.

Covers: (a) protected routes still 401 without credentials in normal mode,
(b) they pass without credentials when ``auth_disabled=true`` (via the
``app.dependency_overrides[get_settings]`` pattern), (c) admin credentials
become optional at Settings construction only when the flag is set, and
(d) webhook HMAC verification is unaffected by the flag.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.api.routes.interactions import get_workflow_client
from aegis.config import Settings
from fastapi.testclient import TestClient
from pydantic import ValidationError

_BASE = {
    "database_url": "postgresql://test:test@localhost/test",
    "litellm_url": "https://litellm.test/v1",
}


def _make_app(settings: Settings):
    app = create_app(run_lifespan=False)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.db_pool = AsyncMock()
    app.state.settings = settings
    return app


# ---------------------------------------------------------------------------
# Route behaviour


def test_protected_route_401s_without_creds_normally():
    settings = Settings(**_BASE, admin_username="admin", admin_password="admin")
    client = TestClient(_make_app(settings))
    assert client.get("/api/infra/services").status_code == 401


def test_protected_route_passes_without_creds_when_auth_disabled():
    settings = Settings(**_BASE, auth_disabled=True)
    client = TestClient(_make_app(settings))
    with patch(
        "aegis.services.chat._exec_list_services",
        new=AsyncMock(return_value=json.dumps([{"name": "aegis_core"}])),
    ):
        resp = client.get("/api/infra/services")
    assert resp.status_code == 200
    assert resp.json() == [{"name": "aegis_core"}]


def test_webhook_hmac_still_enforced_when_auth_disabled():
    """auth_disabled must not weaken webhook signature verification."""
    settings = Settings(
        **_BASE,
        auth_disabled=True,
        github_webhook_secret="test-secret-abc",
    )
    app = _make_app(settings)
    # Temporal client resolves as a dependency even on bad-signature requests.
    app.dependency_overrides[get_workflow_client] = lambda: MagicMock()
    client = TestClient(app)
    resp = client.post(
        "/api/webhooks/github",
        content=json.dumps({"action": "opened"}).encode(),
        headers={
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
            "X-GitHub-Event": "pull_request",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Settings behaviour


class _NoEnvFileSettings(Settings):
    model_config = {**Settings.model_config, "env_file": None}


@pytest.fixture
def _clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("AEGIS_"):
            monkeypatch.delenv(k, raising=False)


def test_admin_creds_required_when_auth_enabled(_clean_env):
    with pytest.raises(ValidationError):
        _NoEnvFileSettings(**_BASE)


def test_admin_creds_optional_when_auth_disabled(_clean_env):
    s = _NoEnvFileSettings(**_BASE, auth_disabled=True)
    assert s.auth_disabled is True
    assert s.admin_username == ""
    assert s.admin_password == ""


def test_empty_password_rejected_when_auth_enabled(_clean_env):
    with pytest.raises(ValidationError):
        _NoEnvFileSettings(**_BASE, admin_username="admin", admin_password="")
