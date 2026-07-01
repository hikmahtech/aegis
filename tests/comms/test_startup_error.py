"""Tests: _startup_error helper — channel-aware boot validation (FIX 2).

_startup_error is a pure helper that returns None when the channel is ready
to boot, or an error string when it is not.  It replaces the old flat
`if not telegram_bot_token: return` guard in run() and makes startup channel-aware.

Note: TelegramSettings uses pydantic-settings AliasChoices; field values must be
supplied via environment variables (monkeypatch.setenv) rather than constructor
kwargs, which are ignored for aliased fields.
"""

from __future__ import annotations


def _settings(monkeypatch, **env_vars):
    """Build a TelegramSettings using env vars (the only reliable way with pydantic-settings)."""
    # Clear Slack tokens by default so tests are isolated
    monkeypatch.delenv("AEGIS_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("AEGIS_SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("AEGIS_CHANNEL", raising=False)
    for k, v in env_vars.items():
        monkeypatch.setenv(k, v)
    from aegis_comms.config import TelegramSettings

    return TelegramSettings(_env_file=None)


# ---------------------------------------------------------------------------
# Slack channel
# ---------------------------------------------------------------------------


def test_startup_error_slack_with_both_tokens_is_none(monkeypatch):
    """slack + bot_token + app_token → no error (ready to boot)."""
    from aegis_comms.__main__ import _startup_error

    settings = _settings(
        monkeypatch,
        AEGIS_CHANNEL="slack",
        AEGIS_SLACK_BOT_TOKEN="xoxb-test",
        AEGIS_SLACK_APP_TOKEN="xapp-test",
    )
    assert _startup_error(settings) is None


def test_startup_error_slack_missing_tokens_returns_error(monkeypatch):
    """slack + missing tokens → error string."""
    from aegis_comms.__main__ import _startup_error

    settings = _settings(monkeypatch, AEGIS_CHANNEL="slack")
    err = _startup_error(settings)
    assert err is not None
    assert "slack" in err.lower() or "token" in err.lower()


def test_startup_error_slack_missing_app_token_returns_error(monkeypatch):
    """slack + bot_token present but app_token missing → error (need both)."""
    from aegis_comms.__main__ import _startup_error

    settings = _settings(monkeypatch, AEGIS_CHANNEL="slack", AEGIS_SLACK_BOT_TOKEN="xoxb-test")
    err = _startup_error(settings)
    assert err is not None


# ---------------------------------------------------------------------------
# run() constructs a SlackAdapter directly
# ---------------------------------------------------------------------------


def test_slack_adapter_constructs_from_settings(monkeypatch):
    """run() constructs SlackAdapter(settings) directly (the sole channel)."""
    from aegis_comms.adapters.slack import SlackAdapter

    settings = _settings(
        monkeypatch,
        AEGIS_CHANNEL="slack",
        AEGIS_SLACK_BOT_TOKEN="xoxb-test",
        AEGIS_SLACK_APP_TOKEN="xapp-test",
    )
    adapter = SlackAdapter(settings)
    assert adapter.name == "slack"
