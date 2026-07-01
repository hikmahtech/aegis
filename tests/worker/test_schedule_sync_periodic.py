"""Periodic schedule_sync — re-runs every 300s by default.

Tests use small `interval_seconds` so wall-clock is irrelevant.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from aegis_worker.__main__ import run_periodic_schedule_sync


async def test_periodic_runs_more_than_once(db_pool):
    fake_client = AsyncMock()
    fake_settings = object()
    call_count = 0

    async def _fake_sync(client, pool, tq, settings=None):
        nonlocal call_count
        call_count += 1
        return call_count

    task = asyncio.create_task(
        run_periodic_schedule_sync(
            client=fake_client,
            pool=db_pool,
            task_queue="aegis-main",
            settings=fake_settings,
            interval_seconds=0.05,
            sync_fn=_fake_sync,
        )
    )
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert call_count >= 2


async def test_periodic_swallows_sync_errors(db_pool):
    fake_client = AsyncMock()
    call_count = 0

    async def _flaky_sync(client, pool, tq, settings=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("first-call blow up")
        return call_count

    task = asyncio.create_task(
        run_periodic_schedule_sync(
            client=fake_client,
            pool=db_pool,
            task_queue="aegis-main",
            settings=None,
            interval_seconds=0.05,
            sync_fn=_flaky_sync,
        )
    )
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert call_count >= 2  # didn't die on the first error
