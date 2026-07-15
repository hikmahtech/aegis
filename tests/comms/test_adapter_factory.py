from aegis_comms.config import CommsSettings


def test_channel_defaults_to_slack(monkeypatch):
    monkeypatch.delenv("AEGIS_CHANNEL", raising=False)
    s = CommsSettings(_env_file=None)
    assert s.channel == "slack"


def test_slack_config_fields_default_empty(monkeypatch):
    for var in ("AEGIS_SLACK_BOT_TOKEN", "AEGIS_SLACK_APP_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    s = CommsSettings(_env_file=None)
    assert s.slack_bot_token == ""
    assert s.slack_app_token == ""
