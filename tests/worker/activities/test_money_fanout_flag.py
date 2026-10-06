"""The cutover switch is read per run, and only an explicit false turns the fan-out off."""

import pytest
from aegis_worker.activities.gmail import GmailActivities


@pytest.mark.asyncio
@pytest.mark.parametrize("stored,expected", [
    ("", True), ("true", True), ("TRUE", True), ("false", False), ("0", False),
    ("no", False), ("Off", False),
])
async def test_money_fanout_enabled_reads_the_setting(monkeypatch, stored, expected):
    from aegis.services import integrations_config

    async def fake_read(pool, settings, key):
        assert key == "money_fanout_enabled"
        return stored

    monkeypatch.setattr(integrations_config, "read_integration", fake_read)
    acts = GmailActivities(gmail_credentials_file="", gmail_token_dir="", db_pool=object())
    assert await acts.money_fanout_enabled() is expected


@pytest.mark.asyncio
async def test_no_pool_means_on():
    acts = GmailActivities(gmail_credentials_file="", gmail_token_dir="")
    assert await acts.money_fanout_enabled() is True
