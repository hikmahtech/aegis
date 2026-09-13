"""A dead connector must become hub work, not one Slack message (#571).

`record_connector_health` predates the problem hub. It counted failures in a
settings row, posted ONE Slack event when the count crossed the threshold, and
latched `alerted` so it never spoke again — so a dead integration produced
exactly one notification in its whole lifetime. Calibre was down for two days
that way: `{"alerted": true, "consecutive_failures": 3}` and no problem, no
task, no owner, no timeline.

These tests are about the hub side of the record. The Slack-ping behaviour they
do not re-test is covered by `test_connector_health.py`.

Real test database, and deliberately no comms URL: the hub side must work when
the ping cannot be sent, which is the case the `alerted` flag never handled.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from aegis.services.connector_health import record_connector_health

pytestmark = pytest.mark.asyncio

_PREFIX = "connector_health:"


def _settings():
    # No comms URL: `_send_system_event` returns False without a request, so
    # `alerted` never latches. The hub record must not depend on the ping.
    return type("S", (), {"comms_url": "", "api_key": ""})()


def _name() -> str:
    return f"zzch_{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture(loop_scope="function", autouse=True)
async def _clean(db_pool):
    """The sweep reads EVERY `connector_health:*` row, so a row left by another
    test would join this one's findings. Clear them, and the problems they made."""
    await db_pool.execute("DELETE FROM settings WHERE key LIKE $1", _PREFIX + "zzch_%")
    await db_pool.execute(
        "DELETE FROM problem_events WHERE problem_id IN ("
        "  SELECT id FROM problems WHERE class = 'connectordown' AND subject LIKE 'zzch_%')"
    )
    await db_pool.execute(
        "DELETE FROM problems WHERE class = 'connectordown' AND subject LIKE 'zzch_%'"
    )
    yield


async def _problem(pool, name: str):
    return await pool.fetchrow(
        "SELECT id::text AS id, class, subject, subject_kind, title, status, severity "
        "FROM problems WHERE class = 'connectordown' AND subject = $1",
        name,
    )


async def _fail(pool, name: str, times: int, threshold: int = 3, error: str = "HTTP 500"):
    for _ in range(times):
        await record_connector_health(
            pool, _settings(), name, ok=False, error=error, threshold=threshold
        )


async def test_below_threshold_is_not_a_problem(db_pool):
    """Two failures out of three is noise. The threshold is the settle window,
    which is why a connector problem needs no settle delay of its own."""
    name = _name()
    await _fail(db_pool, name, 2)
    assert await _problem(db_pool, name) is None


async def test_crossing_the_threshold_opens_a_problem(db_pool):
    """The record Calibre never got: a problem the hub can project, own and close."""
    name = _name()
    await _fail(db_pool, name, 3, error="calibre-web answered /opds/new with HTTP 500")

    p = await _problem(db_pool, name)
    assert p is not None, "a connector past its threshold produced no problem"
    assert (p["subject_kind"], p["status"]) == ("integration", "open")
    assert p["title"] == f"Connector {name} is failing"

    # The source is what gives the projected task its owner, and the payload is
    # what a reader needs to act.
    row = await db_pool.fetchrow(
        "SELECT source, payload FROM problem_events WHERE problem_id = $1::uuid "
        "AND kind = 'occurrence' ORDER BY id LIMIT 1",
        p["id"],
    )
    assert row["source"] == "connector"
    payload = dict(row["payload"] or {})
    assert payload["consecutive_failures"] == 3
    assert "HTTP 500" in payload["last_error"]


async def test_recovery_resolves_the_problem_without_relying_on_the_alerted_flag(db_pool):
    """The heart of #571. `alerted` could not tell "recovered" from "nobody
    looked"; the watchdog seam can, because recovery is the absence of a finding.

    Falsifiable: drop the `_reconcile` call from the recovery path and the
    problem stays open for ever, which is the state Calibre was found in.
    """
    name = _name()
    await _fail(db_pool, name, 3)
    opened = await _problem(db_pool, name)
    assert opened["status"] == "open"

    await record_connector_health(db_pool, _settings(), name, ok=True)

    assert (await _problem(db_pool, name))["status"] == "resolved"
    state = await db_pool.fetchval("SELECT value FROM settings WHERE key = $1", _PREFIX + name)
    assert state["down"] is False and state["consecutive_failures"] == 0


async def test_one_connector_recovering_does_not_resolve_another(db_pool):
    """The bug the obvious implementation has, and the reason the sweep reads
    every `connector_health:*` row rather than the one being recorded.

    `reconcile_findings` resolves any problem of its classes that is absent from
    the findings it is handed. Hand it one connector's state and every OTHER
    connector's live problem is "recovered" — so a healthy Calibre would close a
    dead Miniflux.

    Falsifiable: build the findings from the connector being recorded instead of
    from the whole set, and `down`'s problem is resolved by `up`'s recovery.
    """
    down, up = _name(), _name()
    await _fail(db_pool, down, 3)
    await _fail(db_pool, up, 3)
    assert (await _problem(db_pool, down))["status"] == "open"
    assert (await _problem(db_pool, up))["status"] == "open"

    await record_connector_health(db_pool, _settings(), up, ok=True)

    assert (await _problem(db_pool, up))["status"] == "resolved"
    assert (await _problem(db_pool, down))["status"] == "open", (
        "one connector's recovery resolved another connector's problem"
    )


async def test_a_row_written_before_down_existed_is_still_a_finding(db_pool):
    """Calibre's live row on the day this was written was
    `{"alerted": true, "consecutive_failures": 3}` — no `down` key, because the
    key did not exist yet. Such a row must not read as healthy, or the outage it
    records would be resolved by the first sweep after the deploy.
    """
    legacy, trigger = _name(), _name()
    await db_pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW())",
        _PREFIX + legacy,
        {"alerted": True, "consecutive_failures": 3, "last_error": "HTTP 500"},
    )
    # Any record runs the sweep; use a second connector so `legacy` is untouched.
    await _fail(db_pool, trigger, 1)

    p = await _problem(db_pool, legacy)
    assert p is not None and p["status"] == "open"
