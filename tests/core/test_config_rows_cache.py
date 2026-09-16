"""A failed read is answered, never cached.

`SettingsRow.get` is on hot paths — every mail classified reads
`email_triage_rules`, every task clarified reads `gtd_rules` and
`content_routes`. Those modules used to read the row with no cache at all, so a
blip cost one call. Caching a blip's answer would instead hold "no sender
overrides" for the full TTL after the database came back, and an override is
what carries the `financial`/`payments` tags the money fan-out keys on (#263).

Falsifiable: assign `self._cached` on the `_UNREADABLE` branch of
`SettingsRow.get` and `test_a_failed_read_is_not_cached` fails.
"""

from __future__ import annotations

import pytest
from aegis.services import alert_remediation, email_rules
from aegis.services.config_rows import SettingsRow
from aegis.services.settings_store import setting_exists

ROW = SettingsRow("test_config_rows_cache", lambda v: {"v": (v or {}).get("v", "default")}, dict)


class _BrokenPool:
    """Every read raises, and says how many times it was asked."""

    def __init__(self) -> None:
        self.reads = 0

    async def fetchval(self, *args, **kwargs):
        self.reads += 1
        raise RuntimeError("connection reset")


class _CountingPool:
    """A read that works, so the caching path stays covered."""

    def __init__(self, value) -> None:
        self.value = value
        self.reads = 0

    async def fetchval(self, *args, **kwargs):
        self.reads += 1
        return self.value


async def test_a_failed_read_is_not_cached():
    ROW.clear_cache()
    pool = _BrokenPool()

    assert await ROW.get(pool) == {"v": "default"}
    assert await ROW.get(pool) == {"v": "default"}

    assert pool.reads == 2, "the second call served a cached failure instead of retrying"


async def test_a_good_read_still_is_cached():
    ROW.clear_cache()
    pool = _CountingPool({"v": "stored"})

    assert await ROW.get(pool) == {"v": "stored"}
    assert await ROW.get(pool) == {"v": "stored"}

    assert pool.reads == 1, "the cache stopped working"
    ROW.clear_cache()


async def test_a_pool_less_read_is_not_cached_either():
    """A caller with no pool (a unit test, a worker before bootstrap) must not
    poison the cache for the caller that has one."""
    ROW.clear_cache()
    assert await ROW.get(None) == {"v": "default"}

    pool = _CountingPool({"v": "stored"})
    assert await ROW.get(pool) == {"v": "stored"}
    ROW.clear_cache()


async def test_raw_does_not_swallow_a_failed_read():
    """`raw` backs the forms that report what IS stored. Answering "nothing" on
    a failed read would show an empty form over a configured deployment."""
    with pytest.raises(RuntimeError):
        await ROW.raw(_BrokenPool())


async def test_email_rules_keeps_its_overrides_after_a_blip(db_pool):
    """The lane this protects, end to end."""
    email_rules.ROW.clear_cache()
    await db_pool.execute("DELETE FROM settings WHERE key = $1", email_rules.SETTINGS_KEY)
    try:
        await email_rules.save_email_rules(
            db_pool, {"sender_overrides": {"biller@example.com": "important_read"}}
        )
        email_rules.ROW.clear_cache()

        blip = await email_rules.get_email_rules(_BrokenPool())
        assert blip["sender_overrides"] == {}, "a failed read must answer the empty defaults"

        back = await email_rules.get_email_rules(db_pool)
        assert "biller@example.com" in back["sender_overrides"], (
            "the blip's answer was cached — every mail for the next 30s loses its tags"
        )
    finally:
        await db_pool.execute("DELETE FROM settings WHERE key = $1", email_rules.SETTINGS_KEY)
        email_rules.ROW.clear_cache()


async def test_stored_means_a_row_exists(db_pool):
    """`alert_remediation.stored` is a fact about the row, not about its value.
    A row holding the JSON scalar `null` reads back as None — the same as no
    row — so `get_setting` alone cannot answer it."""
    key = alert_remediation.SETTINGS_KEY
    await db_pool.execute("DELETE FROM settings WHERE key = $1", key)
    try:
        assert (await alert_remediation.get_alert_remediation(db_pool))["stored"] is False

        await db_pool.execute("INSERT INTO settings (key, value) VALUES ($1, 'null'::jsonb)", key)
        assert await setting_exists(db_pool, key) is True
        view = await alert_remediation.get_alert_remediation(db_pool)
        assert view["stored"] is True
        assert view["repeat_window_minutes"] == alert_remediation.DEFAULT_REPEAT_WINDOW_MINUTES
    finally:
        await db_pool.execute("DELETE FROM settings WHERE key = $1", key)
        alert_remediation.ROW.clear_cache()


async def test_alert_remediation_raises_on_a_failed_read():
    """It used to read with a bare `fetchrow`, so a database outage was a 500.
    Answering 200 with the defaults and `stored: false` would read as "nothing
    is configured" — on the row that gates a forced restart."""
    with pytest.raises(RuntimeError):
        await alert_remediation.get_alert_remediation(_BrokenPool())
