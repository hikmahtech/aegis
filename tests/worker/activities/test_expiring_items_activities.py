"""ExpiringItemsActivities — claiming expiry alerts through the dedup ledger.

Real Postgres (the session's freshly-migrated test database via `db_pool`), no
mocks: the whole point of these tests is the unique index on
`life.expiring_item_alerts (item_id, threshold_days, expires_on)`, which a fake
pool could not enforce.

Every window assertion is anchored to the DATABASE's CURRENT_DATE — `due_within`
compares against the server clock, and Python's `date.today()` disagrees with it
for part of every day whenever the test host is not UTC.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import asyncpg
import pytest
import pytest_asyncio
from aegis.db import run_migrations
from aegis.services import expiring_items as svc
from aegis_worker.activities.expiring_items import (
    ExpiringItemsActivities,
    format_expiry_prompt,
)
from temporalio.testing import ActivityEnvironment

# Prefix every fixture row so the assertions can't be satisfied (or broken) by
# rows another test file left behind in the shared database.
PREFIX = "zzradaract-"
AGENT = "zzradaract-agent"


async def _wipe(pool: asyncpg.Pool) -> None:
    # Children before parents: the alert ledger is a hard FK onto the items.
    await pool.execute(
        "DELETE FROM life.expiring_item_alerts WHERE item_id IN "
        "(SELECT id FROM life.expiring_items WHERE title LIKE $1)",
        f"{PREFIX}%",
    )
    await pool.execute("DELETE FROM life.expiring_items WHERE title LIKE $1", f"{PREFIX}%")
    await pool.execute("DELETE FROM notification_log WHERE agent_id = $1", AGENT)


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await run_migrations(db_pool)
    await _wipe(db_pool)
    yield db_pool
    await _wipe(db_pool)


@pytest_asyncio.fixture(loop_scope="function")
async def today(pool):
    """The database's idea of today — the clock `due_within` actually uses."""
    return await pool.fetchval("SELECT CURRENT_DATE")


@pytest_asyncio.fixture(loop_scope="function")
async def acts(pool):
    return ExpiringItemsActivities(db_pool=pool)


async def _mk(pool, today, *, title: str, days: int, lead=(30, 7, 1), notes=None) -> dict:
    return await svc.create_expiring_item(
        pool,
        {
            "kind": "passport",
            "title": f"{PREFIX}{title}",
            "expires_on": today + timedelta(days=days),
            "lead_days": list(lead),
            "notes": notes,
        },
    )


async def _ledger(pool, item_id) -> list[int]:
    rows = await pool.fetch(
        "SELECT threshold_days FROM life.expiring_item_alerts "
        "WHERE item_id = $1 ORDER BY threshold_days",
        item_id,
    )
    return [r["threshold_days"] for r in rows]


async def _claim(acts, *, lookahead=400, max_alerts=5) -> list[dict]:
    return await ActivityEnvironment().run(acts.claim_due_alerts, lookahead, max_alerts)


# --------------------------------------------------------------------------
# format_expiry_prompt
# --------------------------------------------------------------------------


def test_prompt_wording_and_escaping():
    future = format_expiry_prompt(
        title="Passport", kind="passport", days_left=7, expires_on="2026-08-08"
    )
    assert "expires in <b>7 days</b>" in future
    assert "2026-08-08" in future

    assert "expires in <b>1 day</b>" in format_expiry_prompt(
        title="x", kind="k", days_left=1, expires_on="2026-01-01"
    )
    assert "expires <b>today</b>" in format_expiry_prompt(
        title="x", kind="k", days_left=0, expires_on="2026-01-01"
    )
    assert "expired <b>3 days ago</b>" in format_expiry_prompt(
        title="x", kind="k", days_left=-3, expires_on="2026-01-01"
    )

    # Operator free text is HTML-escaped: html_to_mrkdwn treats a raw `<` as
    # markup, and a single stray angle bracket breaks the whole Slack card.
    escaped = format_expiry_prompt(
        title="A<b>hax", kind="k&k", days_left=2, expires_on="2026-01-01", notes="1 < 2"
    )
    assert "A&lt;b&gt;hax" in escaped
    assert "k&amp;k" in escaped
    assert "1 &lt; 2" in escaped


# --------------------------------------------------------------------------
# claim_due_alerts
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crossed_threshold_claims_alert_and_retires_wider_ones(pool, today, acts):
    """5 days out with {30,7,1}: one card for the TIGHTEST crossed threshold,
    and the already-passed 30 is retired in the same claim so it cannot fire
    tomorrow as a stale 'expires in 30 days' alert."""
    item = await _mk(pool, today, title="passport", days=5)

    alerts = await _claim(acts)

    assert len(alerts) == 1
    assert alerts[0]["item_id"] == str(item["id"])
    assert alerts[0]["threshold_days"] == 7
    assert alerts[0]["days_left"] == 5
    assert "expires in <b>5 days</b>" in alerts[0]["prompt"]
    assert await _ledger(pool, item["id"]) == [7, 30]


@pytest.mark.asyncio
async def test_second_claim_same_cycle_returns_nothing(pool, today, acts):
    """The dedup core: a threshold alerts once per expiry cycle, ever."""
    item = await _mk(pool, today, title="visa", days=5)

    first = await _claim(acts)
    second = await _claim(acts)

    assert len(first) == 1
    assert second == []
    assert await _ledger(pool, item["id"]) == [7, 30]


@pytest.mark.asyncio
async def test_concurrent_claims_produce_exactly_one_alert(pool, today, acts):
    """Two runs racing on the same item: the unique index arbitrates, so
    exactly one of them gets rows back and only one card is raised."""
    item = await _mk(pool, today, title="racing-licence", days=5)

    a, b = await asyncio.gather(_claim(acts), _claim(acts))

    assert sorted([len(a), len(b)]) == [0, 1], f"a={a} b={b}"
    assert await _ledger(pool, item["id"]) == [7, 30]


@pytest.mark.asyncio
async def test_no_crossed_threshold_claims_nothing(pool, today, acts):
    item = await _mk(pool, today, title="far-off", days=90)

    assert await _claim(acts) == []
    assert await _ledger(pool, item["id"]) == []


@pytest.mark.asyncio
async def test_next_threshold_fires_on_a_later_pass(pool, today, acts):
    """30-day warning today, 7-day warning when the item moves closer — the
    ledger must not suppress a threshold that has NOT fired yet."""
    item = await _mk(pool, today, title="insurance", days=30)

    first = await _claim(acts)
    assert [a["threshold_days"] for a in first] == [30]

    # Simulate the calendar advancing by moving the expiry closer. Same
    # expires_on cycle key is NOT preserved here on purpose — see the renewal
    # test below for that; this asserts a fresh cycle's 7/1 still fire.
    await pool.execute(
        "UPDATE life.expiring_items SET expires_on = CURRENT_DATE + 6 WHERE id = $1",
        item["id"],
    )
    second = await _claim(acts)
    assert [a["threshold_days"] for a in second] == [7]


@pytest.mark.asyncio
async def test_renewal_rearms_every_threshold(pool, today, acts):
    """Moving expires_on forward is the whole renewal story: the dedup key
    includes expires_on, so the new cycle re-arms with no extra bookkeeping."""
    item = await _mk(pool, today, title="domain", days=5)
    assert len(await _claim(acts)) == 1
    assert await _claim(acts) == []

    await svc.update_expiring_item(
        pool, item["id"], {"expires_on": today + timedelta(days=3)}
    )
    renewed = await _claim(acts)

    assert len(renewed) == 1
    assert renewed[0]["threshold_days"] == 7
    # Old cycle's rows survive (the ledger is never pruned); the new cycle
    # added its own.
    rows = await pool.fetch(
        "SELECT DISTINCT expires_on FROM life.expiring_item_alerts WHERE item_id = $1",
        item["id"],
    )
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_already_expired_item_still_alerts_once(pool, today, acts):
    item = await _mk(pool, today, title="lapsed", days=-12)

    alerts = await _claim(acts)

    assert len(alerts) == 1
    assert alerts[0]["days_left"] == -12
    assert alerts[0]["threshold_days"] == 1
    assert "expired <b>12 days ago</b>" in alerts[0]["prompt"]
    # Every threshold retired at once — a long-overdue item is one alarm, not
    # three days of them.
    assert await _ledger(pool, item["id"]) == [1, 7, 30]
    assert await _claim(acts) == []


@pytest.mark.asyncio
async def test_max_alerts_caps_the_run_and_leaves_surplus_unclaimed(pool, today, acts):
    """The card gate. Cards bypass the notification budget, so a registry that
    suddenly goes overdue must not fire one card per item — and the surplus
    must stay CLAIMABLE, otherwise the cap would silently burn alerts."""
    a = await _mk(pool, today, title="a-soonest", days=1)
    b = await _mk(pool, today, title="b-later", days=3)

    first = await _claim(acts, max_alerts=1)
    assert [x["item_id"] for x in first] == [str(a["id"])]
    assert await _ledger(pool, b["id"]) == []

    second = await _claim(acts, max_alerts=1)
    assert [x["item_id"] for x in second] == [str(b["id"])]


@pytest.mark.asyncio
async def test_lookahead_window_bounds_the_scan(pool, today, acts):
    """`lookahead_days` is the outer window: an item beyond it is invisible
    even though its own lead_days would have crossed."""
    await _mk(pool, today, title="long-lead", days=200, lead=(365,))

    assert await _claim(acts, lookahead=30) == []
    assert len(await _claim(acts, lookahead=400)) == 1


@pytest.mark.asyncio
async def test_notes_ride_along_in_the_card(pool, today, acts):
    await _mk(pool, today, title="with-notes", days=1, notes="renew at the consulate")

    alerts = await _claim(acts)

    assert "renew at the consulate" in alerts[0]["prompt"]


# --------------------------------------------------------------------------
# record_expiry_cards
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_expiry_cards_counts_against_the_budget(pool, acts):
    """Interaction cards never pass through safe_send_message, so without this
    the daily notification budget under-counts them entirely."""
    await ActivityEnvironment().run(acts.record_expiry_cards, AGENT, 2, 1)

    rows = await pool.fetch(
        "SELECT log_event, sent FROM notification_log WHERE agent_id = $1", AGENT
    )
    assert len(rows) == 3
    assert {r["log_event"] for r in rows} == {"expiry_card"}
    assert sorted(r["sent"] for r in rows) == [False, True, True]


@pytest.mark.asyncio
async def test_record_expiry_cards_survives_a_dead_pool(pool):
    """Accounting is best-effort. The cards are already out on the channel by
    the time this runs, so a DB hiccup here must not fail the whole run."""

    class _DeadPool:
        async def execute(self, *args, **kwargs):
            raise asyncpg.PostgresConnectionError("pool is gone")

    broken = ExpiringItemsActivities(db_pool=_DeadPool())
    await ActivityEnvironment().run(broken.record_expiry_cards, AGENT, 2, 0)

    assert await pool.fetchval(
        "SELECT count(*) FROM notification_log WHERE agent_id = $1", AGENT
    ) == 0
