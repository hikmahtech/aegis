"""WorldWatchActivities: Raphael's daily watch over Quantamentry (#676).

One activity. The connection is read from the DB on every run, so an admin
save applies without a worker restart. The run itself is `world_watch.run`.
"""

from __future__ import annotations

from typing import Any

import structlog
from aegis.connectors.quantamentry import QuantamentryClient
from aegis.services import integrations_config, world_watch
from aegis.services.user_time import user_now
from temporalio import activity

logger = structlog.get_logger()


class WorldWatchActivities:
    def __init__(self, *, db_pool: Any, settings: Any) -> None:
        self.db_pool = db_pool
        self.settings = settings

    @activity.defn
    async def world_watch_tick(self, config: dict) -> dict:
        url = await integrations_config.read_integration(self.db_pool, self.settings, "quantamentry_url")
        key = await integrations_config.read_integration(self.db_pool, self.settings, "quantamentry_api_key")
        if not url or not key:
            logger.info("world_watch_unconfigured")
            return {"skipped": "unconfigured"}
        client = QuantamentryClient(url, key, db_pool=self.db_pool)
        try:
            return await world_watch.run(
                self.db_pool,
                client,
                world_watch.WatchConfig.from_config(config),
                today=(await user_now(self.db_pool)).date(),
                settings=self.settings,
            )
        finally:
            await client.close()
