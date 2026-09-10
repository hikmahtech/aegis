"""The worker must not serve activities against a schema Core has not applied
yet (#445).

The stack rolls core and worker at the same time and only core migrates. On the
2026-09-08 deploy the worker came up at 10:00:14 and `check_llm_budget` fired at
10:00:17, three seconds before the migration adding the column it reads. The
failures are indistinguishable from a real fault in the alert stream, and the
window widens with every migration a release carries.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import asyncpg
import pytest
from aegis.db import wait_for_migrations


@pytest.fixture
def migrations(tmp_path: Path) -> Path:
    for name in ("001_a.sql", "002_b.sql", "003_c.sql"):
        (tmp_path / name).write_text("SELECT 1;")
    return tmp_path


class _Pool:
    """A database that applies one more migration on each poll."""

    def __init__(self, applied: list[str], reveal: list[str] | None = None):
        self._applied = list(applied)
        self._reveal = list(reveal or [])
        self.polls = 0

    async def fetch(self, _sql: str, wanted: list[str]):
        self.polls += 1
        rows = [{"filename": n} for n in self._applied if n in wanted]
        if self._reveal:
            self._applied.append(self._reveal.pop(0))
        return rows


class _UnmigratedPool:
    """A database Core has never touched: `schema_migrations` does not exist."""

    def __init__(self):
        self.polls = 0

    async def fetch(self, _sql: str, _wanted: list[str]):
        self.polls += 1
        raise asyncpg.exceptions.UndefinedTableError('relation "schema_migrations" does not exist')


@pytest.mark.asyncio
async def test_a_schema_already_up_to_date_does_not_wait(migrations):
    pool = _Pool(["001_a.sql", "002_b.sql", "003_c.sql"])
    started = time.monotonic()
    assert await wait_for_migrations(pool, migrations, poll_s=5.0) is True
    assert pool.polls == 1
    assert time.monotonic() - started < 1.0, "it slept when there was nothing to wait for"


@pytest.mark.asyncio
async def test_it_waits_for_the_migration_that_has_not_landed_yet(migrations):
    """The real shape of the deploy: core is mid-run and the last file lands a
    few seconds after the worker's pool opens."""
    pool = _Pool(["001_a.sql"], reveal=["002_b.sql", "003_c.sql"])
    assert await wait_for_migrations(pool, migrations, poll_s=0.01) is True
    assert pool.polls == 3


@pytest.mark.asyncio
async def test_a_database_with_no_migrations_table_at_all_is_not_an_error(migrations):
    """A fresh install, where Core has not run once. Not ready, not a crash —
    an `UndefinedTableError` escaping here would take the worker down on the
    one boot where waiting is most obviously right."""
    pool = _UnmigratedPool()
    assert await wait_for_migrations(pool, migrations, timeout_s=0.05, poll_s=0.01) is False
    assert pool.polls >= 1


@pytest.mark.asyncio
async def test_it_fails_open_rather_than_refusing_to_boot(migrations):
    """A worker that never starts is worse than one that starts early: the
    flow-health watchdog runs in the worker too, so blocking for ever would
    silence the very thing that reports the problem."""
    pool = _Pool(["001_a.sql"])
    started = time.monotonic()
    assert await wait_for_migrations(pool, migrations, timeout_s=0.05, poll_s=0.01) is False
    assert time.monotonic() - started < 2.0


@pytest.mark.asyncio
async def test_an_older_image_waits_only_for_what_it_ships(tmp_path):
    """A worker rolled BACK must not sit waiting for migrations it neither
    ships nor needs. Waiting on the image's own directory is what gives that
    for free — waiting on "is Core up" would not."""
    (tmp_path / "001_a.sql").write_text("SELECT 1;")
    pool = _Pool(["001_a.sql"])  # the DB is AHEAD; 002 and 003 are not ours
    assert await wait_for_migrations(pool, tmp_path, timeout_s=0.05, poll_s=0.01) is True
    assert pool.polls == 1


@pytest.mark.asyncio
async def test_no_migrations_directory_is_not_something_to_wait_for(tmp_path):
    """A dev worker started from another working directory."""
    pool = _Pool([])
    assert await wait_for_migrations(pool, tmp_path / "nope", timeout_s=0.05) is True
    assert pool.polls == 0


@pytest.mark.asyncio
async def test_the_real_migrations_directory_is_satisfied_by_the_test_database(db_pool):
    """The end of the argument: `tests/conftest.py` builds each test database by
    running `migrations/`, so the repo's own directory must come back ready.
    A filename-matching bug — a path instead of a name, a stale table name —
    would hang every worker boot for a minute, and only in production."""
    root = Path(__file__).resolve().parents[2] / "migrations"
    assert root.is_dir()
    assert await asyncio.wait_for(wait_for_migrations(db_pool, root, poll_s=0.01), timeout=5)
