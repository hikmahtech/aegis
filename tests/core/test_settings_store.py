"""The two `settings` statements, against a real row.

`get_setting` returning None has to mean "no row" and nothing else — every
caller in core and the worker reads it that way — and `put_setting` has to
replace an existing row rather than raise on its primary key.
"""

from __future__ import annotations

from aegis.services.settings_store import get_setting, put_setting


async def test_missing_key_reads_as_none(db_pool):
    assert await get_setting(db_pool, "no_such_setting_row") is None


async def test_put_writes_then_replaces(db_pool):
    key = "test_settings_store_row"
    await put_setting(db_pool, key, {"a": 1})
    assert await get_setting(db_pool, key) == {"a": 1}

    await put_setting(db_pool, key, {"b": 2})
    assert await get_setting(db_pool, key) == {"b": 2}

    # One row, not two: the upsert is keyed on `key`.
    assert await db_pool.fetchval("SELECT count(*) FROM settings WHERE key = $1", key) == 1
    await db_pool.execute("DELETE FROM settings WHERE key = $1", key)


async def test_a_scalar_round_trips(db_pool):
    """A bare string is a legitimate value (`user_timezone` stores one)."""
    key = "test_settings_store_scalar"
    await put_setting(db_pool, key, "Asia/Kolkata")
    assert await get_setting(db_pool, key) == "Asia/Kolkata"
    await db_pool.execute("DELETE FROM settings WHERE key = $1", key)
