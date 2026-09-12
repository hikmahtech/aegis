"""TradingDeskActivities.desk_tick: the wiring between settings and the run."""

from __future__ import annotations

import pytest_asyncio
from aegis.connectors.finance import FinanceConnector
from aegis.services import trading_desk
from aegis.services.integrations_config import save_integration
from aegis_worker.activities.trading_desk import TradingDeskActivities
from temporalio.testing import ActivityEnvironment

_KEYS = ["integration:ansaar_url", "integration:ansaar_service_secret"]


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key = ANY($1::text[])", _KEYS)
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = ANY($1::text[])", _KEYS)


async def test_an_unconfigured_desk_does_nothing(pool, test_settings, monkeypatch):
    called = []
    monkeypatch.setattr(trading_desk, "run_tick", lambda *a, **k: called.append(1))
    act = TradingDeskActivities(db_pool=pool, settings=test_settings)
    assert await ActivityEnvironment().run(act.desk_tick) == {"skipped": "unconfigured"}
    assert called == []


async def test_a_configured_desk_runs_with_the_saved_connection(pool, test_settings, monkeypatch):
    await save_integration(pool, test_settings, "ansaar_url", "http://ansaar.test")
    await save_integration(pool, test_settings, "ansaar_service_secret", "s3cret")
    seen: dict = {}

    async def fake_run_tick(p, *, ansaar, finance, **kw):
        seen.update(url=ansaar._url, secret=ansaar._secret, finance=finance)
        return {"day": "2026-09-11"}

    monkeypatch.setattr(trading_desk, "run_tick", fake_run_tick)
    act = TradingDeskActivities(db_pool=pool, settings=test_settings)
    assert await ActivityEnvironment().run(act.desk_tick) == {"day": "2026-09-11"}
    assert (seen["url"], seen["secret"]) == ("http://ansaar.test", "s3cret")
    assert isinstance(seen["finance"], FinanceConnector)
