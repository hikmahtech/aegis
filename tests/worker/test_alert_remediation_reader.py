"""The worker reads the auto-restart window through the core merge (#558).

What the Problems page saves (`save_alert_remediation`, strict) is what the
worker restarts by, and a row written by hand that the page would refuse still
reads as the default rather than breaking the lane.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from aegis.services import alert_remediation as core
from aegis_worker.activities.alerts import (
    ALERT_REMEDIATION_SETTINGS_KEY,
    DEFAULT_RESTART_REPEAT_WINDOW_MINUTES,
    restart_repeat_window_minutes,
)


def test_the_worker_constants_are_the_core_ones():
    assert ALERT_REMEDIATION_SETTINGS_KEY == core.SETTINGS_KEY
    assert DEFAULT_RESTART_REPEAT_WINDOW_MINUTES == core.DEFAULT_REPEAT_WINDOW_MINUTES == 60


@pytest_asyncio.fixture(loop_scope="function")
async def clean(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key = 'alert_remediation'")
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = 'alert_remediation'")


async def test_a_window_saved_on_the_problems_page_is_what_the_worker_reads(clean):
    assert await restart_repeat_window_minutes(clean) == 60
    await core.save_alert_remediation(clean, {"repeat_window_minutes": 15})
    assert await restart_repeat_window_minutes(clean) == 15
    await core.save_alert_remediation(clean, {"repeat_window_minutes": 0})
    assert await restart_repeat_window_minutes(clean) == 0


@pytest.mark.parametrize(
    "value", ["soon", {"repeat_window_minutes": -3}, {"repeat_window_minutes": True}, [1]]
)
async def test_a_hand_written_bad_row_reads_as_the_default(clean, value):
    await clean.execute(
        "INSERT INTO settings (key, value) VALUES ('alert_remediation', $1)", value
    )
    assert await restart_repeat_window_minutes(clean) == 60


async def test_no_pool_is_the_default():
    assert await restart_repeat_window_minutes(None) == 60
