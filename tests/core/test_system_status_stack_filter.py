"""System Monitoring scopes `docker service ls` to AEGIS's own swarm stack."""

from aegis.api.routes.system_status import _auth_mode, _parse_service_lines, _stack_filter_args
from aegis.config import Settings

_TEST_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "n8n_ui_url": "https://n8n.example.com",
    "n8n_webhook_secret": "test-secret",
    "admin_username": "admin",
    "admin_password": "admin",
}


def _settings(**overrides) -> Settings:
    return Settings(**{**_TEST_SETTINGS, **overrides})


def test_stack_filter_args_scopes_to_namespace_label():
    assert _stack_filter_args("aegis") == [
        "--filter",
        "label=com.docker.stack.namespace=aegis",
    ]


def test_stack_filter_args_blank_shows_all():
    # Empty stack = escape hatch: no filter, list every service.
    assert _stack_filter_args("") == []


def test_parse_service_lines_derives_stack_from_name():
    rows = _parse_service_lines("aegis_core 1/1 img:latest\naegis_worker 2/2 img:latest\n")
    assert [r["name"] for r in rows] == ["aegis_core", "aegis_worker"]
    assert {r["stack"] for r in rows} == {"aegis"}


# auth_mode surfaces an auth-disabled deployment in the admin UI (#88).


def test_auth_mode_disabled_wins_over_configured_credentials():
    # The flag bypasses verify_auth outright, so present credentials are moot —
    # reporting "basic" here would hide the very hazard we surface.
    assert _auth_mode(_settings(auth_disabled=True, api_key="k")) == "disabled"


def test_auth_mode_basic():
    assert _auth_mode(_settings(api_key="")) == "basic"


def test_auth_mode_basic_plus_api_key():
    assert _auth_mode(_settings(api_key="k")) == "basic+api_key"


def test_auth_mode_api_key_only():
    assert _auth_mode(_settings(auth_disabled=True, admin_username="", admin_password="", api_key="k")) == "disabled"
    # auth_disabled=False + no basic creds can't boot (config validator), so
    # build the api_key-only case by clearing basic on an instantiated object.
    settings = _settings(api_key="k")
    settings.admin_username = ""
    settings.admin_password = ""
    assert _auth_mode(settings) == "api_key"
