"""Tests for AEGIS v2 config module."""

import os

import pytest
from aegis.config import Settings
from pydantic import ValidationError

_REQUIRED = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.test/v1",
    "temporal_ui_url": "https://temporal.test",
    "n8n_ui_url": "https://n8n.test",
    "admin_username": "admin",
    "admin_password": "admin",
    "n8n_webhook_secret": "test-secret",
}


def test_settings_with_required_values():
    """Settings loads when required values are supplied."""
    s = Settings(**_REQUIRED)
    assert s.database_url == _REQUIRED["database_url"]
    assert s.api_key == ""
    assert s.admin_username == "admin"
    assert s.litellm_url == _REQUIRED["litellm_url"]


def test_settings_missing_required_field_raises(monkeypatch):
    """Settings raises ValidationError when a required field is missing."""
    # Clear any AEGIS_ env vars AND suppress config/.env so this test is
    # hermetic regardless of local shell state or repo-local dotenv file.
    for k in list(os.environ):
        if k.startswith("AEGIS_"):
            monkeypatch.delenv(k, raising=False)

    class _NoEnvFileSettings(Settings):
        model_config = {**Settings.model_config, "env_file": None}

    with pytest.raises(ValidationError):
        # Drop admin_password so validation fails.
        partial = {k: v for k, v in _REQUIRED.items() if k != "admin_password"}
        _NoEnvFileSettings(**partial)


def test_llm_and_temporal_ui_optional(monkeypatch):
    """litellm_url + temporal_ui_url are optional now (LLM is configured in the
    admin UI; temporal_ui_url is a link with a default). channel defaults to web."""
    for k in list(os.environ):
        if k.startswith("AEGIS_"):
            monkeypatch.delenv(k, raising=False)

    class _NoEnvFileSettings(Settings):
        model_config = {**Settings.model_config, "env_file": None}

    s = _NoEnvFileSettings(
        database_url="postgresql://x", admin_username="u", admin_password="p"
    )
    assert s.litellm_url == ""
    assert s.temporal_ui_url == "http://localhost:8233"
    assert s.channel == "web"


def test_env_prefix():
    """Settings reads AEGIS_ prefixed env vars."""
    os.environ["AEGIS_API_KEY"] = "test-key-123"
    try:
        s = Settings(**_REQUIRED)
        assert s.api_key == "test-key-123"
    finally:
        del os.environ["AEGIS_API_KEY"]


def test_homelab_settings_defaults(monkeypatch):
    """Homelab settings load with defaults and comma-separated domain list."""
    monkeypatch.setenv("AEGIS_DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("AEGIS_LITELLM_URL", "http://x")
    monkeypatch.setenv("AEGIS_TEMPORAL_UI_URL", "http://x")
    monkeypatch.setenv("AEGIS_ADMIN_USERNAME", "u")
    monkeypatch.setenv("AEGIS_ADMIN_PASSWORD", "p")
    monkeypatch.setenv("AEGIS_HOMELAB_PUBLIC_DOMAINS", "a.example.com,b.example.com")

    s = Settings()
    # homelab_enabled defaults to False — operators must opt in.
    assert s.homelab_enabled is False
    assert s.homelab_docker_context == ""
    assert s.homelab_public_domains == ["a.example.com", "b.example.com"]


def test_homelab_probe_fields_deleted():
    """The owner-specific monitoring probes (Dagster GraphQL, Traefik API,
    NFS backup audit + restore drill) were stripped for OSS — their config
    knobs must no longer exist. Docker-swarm ops + cert radar stay."""
    s = Settings(**_REQUIRED)
    for gone in (
        "homelab_nfs_base_path",
        "homelab_dagster_graphql_url",
        "homelab_traefik_api_url",
        "homelab_restore_drill_host",
        "homelab_restore_drill_disk_min_gb",
        "homelab_drift_sustained_minutes",
        "chat_auto_extract_enabled",
        "money_hygiene_haiku_batch",
        "money_hygiene_cancellation_threshold_multiplier",
        "money_hygiene_alert_thresholds_days",
    ):
        assert not hasattr(s, gone), f"{gone} should have been deleted"


def test_money_hygiene_settings_defaults():
    """Money Hygiene settings load with the spec'd defaults."""
    s = Settings(**_REQUIRED)
    assert s.money_hygiene_enabled is False
    assert s.home_currency == "INR"
    assert "USD" in s.money_hygiene_fx_rates
    assert s.money_hygiene_fx_rates["USD"] == 84.5
    assert s.money_hygiene_fx_rates["EUR"] == 92.0
    assert s.money_hygiene_fx_rates["GBP"] == 108.0
    assert s.money_hygiene_fx_rates["SGD"] == 63.0


def test_comma_separated_list_fields_parse_from_env(monkeypatch):
    """NoDecode-annotated list fields comma-split raw env strings (no JSON).

    Regression guard for the config.py change that replaced the bespoke
    _CustomEnvSource with NoDecode + the model_validator(mode="before")
    splitter — feeding JSON-list strings here would raise.
    """
    for k, v in _REQUIRED.items():
        monkeypatch.setenv(f"AEGIS_{k.upper()}", v)
    monkeypatch.setenv("AEGIS_HOMELAB_PUBLIC_DOMAINS", "a.com,b.com, c.com")
    s = Settings()
    assert s.homelab_public_domains == ["a.com", "b.com", "c.com"]


def test_settings_default_todoist_fields_empty(monkeypatch):
    """Todoist settings default to empty strings; feature is opt-in."""
    # Strip any AEGIS_TODOIST_* vars that may be set in the local shell or .env
    for key in list(os.environ):
        if key.startswith("AEGIS_TODOIST_"):
            monkeypatch.delenv(key, raising=False)

    class _NoEnvFileSettings(Settings):
        model_config = {**Settings.model_config, "env_file": None}

    s = _NoEnvFileSettings(**_REQUIRED)
    assert s.todoist_api_key == ""
    assert s.todoist_webhook_secret == ""


def test_settings_reads_todoist_env_vars(monkeypatch):
    """AEGIS_TODOIST_* env vars populate the settings."""
    for k, v in _REQUIRED.items():
        monkeypatch.setenv(f"AEGIS_{k.upper()}", v)
    monkeypatch.setenv("AEGIS_TODOIST_API_KEY", "test-key")
    monkeypatch.setenv("AEGIS_TODOIST_WEBHOOK_SECRET", "shh")
    s = Settings()
    assert s.todoist_api_key == "test-key"
    assert s.todoist_webhook_secret == "shh"
