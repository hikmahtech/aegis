"""TradingDeskFlow: one weekday-morning run of Maou's paper trading desk.

Everything happens in the `desk_tick` activity, which is idempotent, so a retry
or a manual re-run on the same day changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.shared.retry import FAST


@dataclass
class TradingDeskConfig:
    agent_id: str = "maou"


@workflow.defn(name="TradingDeskFlow")
class TradingDeskFlow:
    @workflow.run
    async def run(self, config: TradingDeskConfig) -> dict:
        return await workflow.execute_activity(
            "desk_tick", start_to_close_timeout=timedelta(minutes=5), retry_policy=FAST
        )
