"""#676 — the world watch: Quantamentry scores and bank calendar → area items."""

from __future__ import annotations

import uuid
from datetime import date

import httpx
import pytest
import pytest_asyncio
import respx
from aegis.connectors.quantamentry import QuantamentryClient, QuantamentryError
from aegis.services import research_topics, world_watch
from aegis.services.world_watch import WatchConfig, watch_items

TODAY = date(2026, 9, 25)
CFG = WatchConfig(countries=("IND", "PAK"), banks=("USA",), link="https://q.example/countries/{iso}")


def _score(iso="IND", **kw):
    return {
        "country_iso": iso, "country_name": {"IND": "India", "PAK": "Pakistan"}.get(iso, iso),
        "composite_score": 52.2, "delta_7d": -0.5, "summary": "on target", "is_current": True,
        "regime_shift": {"direction": None, "tripped_date": None}, **kw,
    }


def _bank(**kw):
    return {
        "country_iso": "USA", "bank": "Federal Reserve", "currency": "USD",
        "next_meeting": "2026-10-28", "last_meeting": "2026-09-16",
        "stance": {"status": "available", "latest_meeting": "2026-09-16", "delta": -0.05}, **kw,
    }


def test_a_quiet_day_files_nothing():
    assert watch_items([_score(), _score("PAK")], [_bank()], CFG, TODAY) == []


def test_a_recent_regime_shift_is_an_item_keyed_on_its_date():
    old = _score("PAK", regime_shift={"direction": "up", "tripped_date": "2026-09-01"})
    new = _score(regime_shift={"direction": "down", "tripped_date": "2026-09-22"})
    (item,) = watch_items([new, old], [], CFG, TODAY)
    assert item["title"] == "India: policy credibility has turned deteriorating (regime shift on 22 Sep)"
    assert item["url"] == "https://q.example/countries/IND#regime-2026-09-22"


def test_a_big_weekly_move_is_one_item_per_week_and_direction():
    (item,) = watch_items([_score(delta_7d=-4.2)], [], CFG, TODAY)
    assert "fell 4.2 points in a week, to 52.2" in item["title"]
    assert item["url"].endswith("#move-down-2026-W39")
    # The next day's run in the same week is the same URL, so it attaches once.
    (again,) = watch_items([_score(delta_7d=-4.6)], [], CFG, date(2026, 9, 26))
    assert again["url"] == item["url"]


def test_unwatched_or_stale_countries_are_ignored():
    rows = [_score("RWA", delta_7d=-10.4), _score(delta_7d=-9, is_current=False)]
    assert watch_items(rows, [], CFG, TODAY) == []


def test_a_watched_bank_meeting_soon_and_a_stance_move_are_items():
    bank = _bank(
        next_meeting="2026-09-30",
        stance={"status": "available", "latest_meeting": "2026-09-20", "delta": 0.35},
    )
    titles = [i["title"] for i in watch_items([], [bank, {**bank, "country_iso": "GBR"}], CFG, TODAY)]
    assert titles == [
        "Federal Reserve sets rates on Wednesday 30 Sep",
        "Federal Reserve turned more hawkish at its 20 Sep meeting",
    ]


def test_config_reads_the_activities_row_leniently():
    cfg = WatchConfig.from_config(
        {"countries": ["ind", " ", "pak"], "banks": "USA", "move_points": "x", "meeting_days": 3}
    )
    assert (cfg.countries, cfg.banks, cfg.move_points, cfg.meeting_days) == (("IND", "PAK"), (), 3.0, 3)
    assert WatchConfig.from_config(None) == WatchConfig()
    # No link: the URL is still unique, just not a web page.
    (item,) = watch_items([_score(delta_7d=5)], [], WatchConfig(countries=("IND",)), TODAY)
    assert item["url"].startswith("quantamentry://IND#move-up-")


@pytest.mark.asyncio
async def test_the_client_sends_the_key_and_never_echoes_it():
    with respx.mock(base_url="http://q") as mock:
        route = mock.get("/api/scores").respond(200, json=[_score()])
        mock.get("/api/cb-calendar").respond(401, json={"detail": "Invalid or missing API key"})
        client = QuantamentryClient("http://q/", "s3cret")
        assert (await client.scores())[0]["country_iso"] == "IND"
        assert route.calls[0].request.headers["X-API-Key"] == "s3cret"
        with pytest.raises(QuantamentryError) as exc:
            await client.cb_calendar()
        assert str(exc.value) == "/api/cb-calendar: HTTP 401" and "s3cret" not in str(exc.value)
        mock.get("/api/status.json").mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(QuantamentryError, match="ConnectError"):
            await client.status()
        await client.close()


# --- the run, on a real database ----------------------------------------------


class _Client:
    def __init__(self, latest="2026-09-24", scores=None):
        self.latest, self._scores = latest, scores or []

    async def status(self):
        return {"latest_score_date": self.latest}

    async def scores(self):
        return self._scores

    async def cb_calendar(self):
        return []


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key LIKE 'connector_health:%'")
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = $1", research_topics.TOPICS_SETTING)
    await db_pool.execute("DELETE FROM settings WHERE key LIKE 'connector_health:%'")


async def _health(pool):
    return await pool.fetchval("SELECT value FROM settings WHERE key = 'connector_health:quantamentry'")


@pytest.mark.asyncio
async def test_stale_scores_file_nothing_and_mark_the_connector_unhealthy(pool):
    out = await world_watch.run(pool, _Client(latest="2026-09-10", scores=[_score(delta_7d=9)]), CFG, today=TODAY)
    assert "stale" in out["error"]
    assert (await _health(pool))["consecutive_failures"] == 1


@pytest.mark.asyncio
async def test_a_run_files_items_once_under_the_watch_topic(pool):
    topic = f"Country watch {uuid.uuid4().hex[:6]}"
    cfg = WatchConfig(countries=("IND",), topic=topic)
    client = _Client(scores=[_score(delta_7d=-4.2)])

    missing = await world_watch.run(pool, client, cfg, today=TODAY)
    assert missing["missing_topic"] is True and missing["attached"] == 0

    await research_topics.track(pool, topic, [topic])
    first = await world_watch.run(pool, client, cfg, today=TODAY)
    again = await world_watch.run(pool, client, cfg, today=TODAY)
    assert (first["items"], first["attached"], again["attached"]) == (1, 1, 0)
    row = await pool.fetchrow(
        "SELECT e.payload FROM problem_events e JOIN problems p ON p.id = e.problem_id "
        "WHERE p.subject = $1 AND e.payload->>'item' = 'true'",
        research_topics.slug(topic),
    )
    assert row["payload"]["origin"] == "quantamentry" and "India" in row["payload"]["title"]


@pytest.mark.asyncio
async def test_nothing_watched_does_nothing(pool):
    assert await world_watch.run(pool, _Client(), WatchConfig(), today=TODAY) == {"skipped": "nothing_watched"}
