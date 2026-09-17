"""Probes that tests/core/test_test_isolation.py runs in a child pytest.

A normal run never collects this file: its name does not match `test_*.py`,
and pytest only runs such a file when it is named on the command line.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path


async def _record_database(db_pool) -> str:
    """Write this run's database name where the parent test can read it."""
    name = await db_pool.fetchval("SELECT current_database()")
    out = os.environ.get("AEGIS_PROBE_DIR")
    if out:
        (Path(out) / os.environ["AEGIS_PROBE_NAME"]).write_text(name)
    return name


async def test_hold_database_until_peers_arrive(db_pool):
    """#325: hold this run's database while every peer run holds its own.

    Each peer writes its marker only after its database exists, so by the time
    this test reads back its token every peer has already created (and, under
    the old per-worker naming, dropped) its database.
    """
    token = f"{os.getpid()}-{time.time_ns()}"
    await db_pool.execute("CREATE TABLE probe_hold (token text)")
    await db_pool.execute("INSERT INTO probe_hold VALUES ($1)", token)
    await _record_database(db_pool)

    out = Path(os.environ["AEGIS_PROBE_DIR"])
    peers = os.environ["AEGIS_PROBE_PEERS"].split(",")
    deadline = time.monotonic() + 90
    while not all((out / peer).exists() for peer in peers):
        assert time.monotonic() < deadline, f"peer runs never arrived: {peers}"
        await asyncio.sleep(0.1)

    assert await db_pool.fetchval("SELECT token FROM probe_hold") == token


async def test_capture_switch_is_on(db_pool):
    """#569: the kill switch is the migration's default, `true`."""
    await _record_database(db_pool)
    value = await db_pool.fetchval(
        "SELECT value FROM settings WHERE key = 'todoist_capture_enabled'"
    )
    assert value is True


async def test_settings_are_as_seeded(db_pool):
    """#569: nothing `probe_settings_leak.py` did is still there."""
    rows = {r["key"]: r["value"] for r in await db_pool.fetch("SELECT key, value FROM settings")}
    assert rows.get("todoist_capture_enabled") is True
    assert "probe_leaked_key" not in rows
    assert rows.get("user_timezone") == "UTC"
