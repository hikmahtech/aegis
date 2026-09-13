"""A probe that leaves `settings` rows behind on purpose (#569).

Run only by tests/core/test_test_isolation.py, in a child pytest, just before
`probe_isolation.py::test_settings_are_as_seeded`. A normal run never collects
it: the file name does not match `test_*.py`.
"""

from __future__ import annotations


async def test_leave_settings_behind(db_pool):
    """Change a row, add a row, delete a row — and restore none of them."""
    await db_pool.execute(
        "UPDATE settings SET value = 'false'::jsonb WHERE key = 'todoist_capture_enabled'"
    )
    await db_pool.execute(
        "INSERT INTO settings (key, value) VALUES ('probe_leaked_key', '1'::jsonb) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )
    await db_pool.execute("DELETE FROM settings WHERE key = 'user_timezone'")
