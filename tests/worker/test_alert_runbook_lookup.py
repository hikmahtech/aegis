"""gather_alert_knowledge's runbook lookup order: the database, then the file (#499).

A deployment keeps runbooks about its own setup (which machines share a power
supply, which service is pinned where) in the `runbooks` table, because the
repo is public. The generic ones ship as files baked into the worker image.
These drive the real activity end to end against the real test database: no
DB mocks. The "database unavailable" case uses a pool that has been closed,
which fails exactly the way a lost connection does.
"""

from __future__ import annotations

import asyncio
import logging

import pytest_asyncio
from aegis.db import create_pool, run_migrations
from aegis.services import runbooks as rb
from aegis_worker.activities import alerts as alerts_mod
from aegis_worker.activities.alerts import AlertActivities
from temporalio.testing import ActivityEnvironment

PREFIX = "zzrbwk"
ALERT = "ZzrbwkNodeDown"  # the alertname as Prometheus sends it
FILE_BODY = "# NodeDown (generic)\n\nFILE BODY: check the node."
DB_BODY = "# NodeDown (this deployment)\n\nDB BODY: check the power domain first."


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM runbooks WHERE name_key LIKE $1", f"{PREFIX}%")
    yield db_pool
    await db_pool.execute("DELETE FROM runbooks WHERE name_key LIKE $1", f"{PREFIX}%")


def _write_file(tmp_path, name=ALERT, body=FILE_BODY):
    (tmp_path / f"{name}.md").write_text(body)
    return str(tmp_path)


async def test_a_database_runbook_wins_over_the_file(pool, tmp_path):
    # Saved under the hub's slug spelling; the alert arrives CamelCase.
    await rb.put_runbook(pool, "zzrbwk-node-down", DB_BODY, updated_by="test")
    act = AlertActivities(db_pool=pool, runbooks_dir=_write_file(tmp_path))

    result = await ActivityEnvironment().run(act.gather_alert_knowledge, "node gone", "", ALERT)

    assert result == f"Runbook:\n{DB_BODY}"


async def test_the_file_answers_when_the_database_has_no_runbook(pool, tmp_path):
    act = AlertActivities(db_pool=pool, runbooks_dir=_write_file(tmp_path))

    result = await ActivityEnvironment().run(act.gather_alert_knowledge, "node gone", "", ALERT)

    assert result == f"Runbook:\n{FILE_BODY}"


async def test_an_unreachable_database_falls_back_to_the_file(test_db_url, tmp_path, caplog):
    dead = await create_pool(test_db_url, min_size=1, max_size=1)
    await dead.close()  # every call now raises, like a dropped connection
    act = AlertActivities(db_pool=dead, runbooks_dir=_write_file(tmp_path))

    with caplog.at_level(logging.WARNING):
        result = await ActivityEnvironment().run(act.gather_alert_knowledge, "node gone", "", ALERT)

    assert result == f"Runbook:\n{FILE_BODY}"
    assert any("runbook_db_read_failed" in r.getMessage() for r in caplog.records)


async def test_a_hung_database_falls_back_to_the_file(test_db_url, tmp_path, monkeypatch, caplog):
    # The pool has no command timeout. Hold its only connection, so the lookup
    # waits forever for one: without its own bound it would sit until the
    # activity's 65s timeout and the investigation would start with nothing.
    monkeypatch.setattr(alerts_mod, "_RUNBOOK_DB_TIMEOUT_S", 0.3)
    stuck = await create_pool(test_db_url, min_size=1, max_size=1)
    held = await stuck.acquire()
    try:
        act = AlertActivities(db_pool=stuck, runbooks_dir=_write_file(tmp_path))
        with caplog.at_level(logging.WARNING):
            # The outer bound turns a missing inner one into a failure, not a hang.
            result = await asyncio.wait_for(
                ActivityEnvironment().run(act.gather_alert_knowledge, "node gone", "", ALERT),
                timeout=10,
            )
    finally:
        await stuck.release(held)
        await stuck.close()

    assert result == f"Runbook:\n{FILE_BODY}"
    assert any("runbook_db_read_failed" in r.getMessage() for r in caplog.records)


async def test_a_stub_row_is_no_runbook_and_the_file_answers(pool, tmp_path):
    # The service refuses a stub on write; a hand-written INSERT can still land
    # one. It must read the same as a stub file: absent.
    await pool.execute(
        "INSERT INTO runbooks (name_key, name, body) VALUES ($1, $2, $3)",
        rb.normalise_name(ALERT),
        ALERT,
        "# NodeDown\n\nTODO: fill in",
    )
    act = AlertActivities(db_pool=pool, runbooks_dir=_write_file(tmp_path))

    result = await ActivityEnvironment().run(act.gather_alert_knowledge, "node gone", "", ALERT)

    assert result == f"Runbook:\n{FILE_BODY}"


async def test_a_database_runbook_needs_no_file_at_all(pool):
    # The homelab-only classes (a Grafana rule title, say) have no file in the
    # repo and never will.
    await rb.put_runbook(pool, "Zzrbwk Pipeline Failure", DB_BODY, updated_by="test")
    act = AlertActivities(db_pool=pool, runbooks_dir="/nonexistent/runbooks")

    result = await ActivityEnvironment().run(
        act.gather_alert_knowledge, "run failed", "", "Zzrbwk Pipeline Failure"
    )

    assert result == f"Runbook:\n{DB_BODY}"
