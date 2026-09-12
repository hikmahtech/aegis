"""TradingDeskActivities: Maou's paper trading desk (spec 2026-09-12-maou-trading-desk-design.md).

One activity. The connection is read from the DB on every run, so an admin save
applies without a worker restart. The run itself is `trading_desk.run_tick`.
"""

from __future__ import annotations

from typing import Any

import structlog
from aegis.connectors.ansaar import AnsaarClient
from aegis.connectors.finance import FinanceConnector
from aegis.services import integrations_config, trading_desk
from temporalio import activity

logger = structlog.get_logger()


class TradingDeskActivities:
    def __init__(self, *, db_pool: Any, settings: Any, finance: FinanceConnector | None = None) -> None:
        self.db_pool = db_pool
        self.settings = settings
        self.finance = finance or FinanceConnector(db_pool=db_pool)

    @activity.defn
    async def desk_tick(self) -> dict:
        url = await integrations_config.read_integration(self.db_pool, self.settings, "ansaar_url")
        secret = await integrations_config.read_integration(self.db_pool, self.settings, "ansaar_service_secret")
        if not url or not secret:
            logger.info("trading_desk_unconfigured")
            return {"skipped": "unconfigured"}
        ansaar = AnsaarClient(url, secret, db_pool=self.db_pool)
        try:
            return await trading_desk.run_tick(self.db_pool, ansaar=ansaar, finance=self.finance)
        finally:
            await ansaar.close()
