"""TenderWatchActivities: the daily GeM tender watch (#673)."""

from __future__ import annotations

from typing import Any

from aegis.connectors.gem import GemClient
from aegis.services import tender_watch
from aegis.services.user_time import user_now
from temporalio import activity


class TenderWatchActivities:
    def __init__(self, *, db_pool: Any, settings: Any) -> None:
        self.db_pool = db_pool
        self.settings = settings

    @activity.defn
    async def tender_watch_tick(self, config: dict) -> dict:
        client = GemClient(db_pool=self.db_pool)
        try:
            return await tender_watch.run(
                self.db_pool,
                client,
                tender_watch.TenderConfig.from_config(config),
                today=(await user_now(self.db_pool)).date(),
                settings=self.settings,
            )
        finally:
            await client.close()
