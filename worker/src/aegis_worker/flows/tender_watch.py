"""TenderWatchFlow: one run of the GeM tender watch (#673). All in the
`tender_watch_tick` activity; a bid already filed is a no-op."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.shared.retry import FAST


@dataclass
class TenderWatchConfig:
    agent_id: str = "raphael"
    # The `tender-watch-daily` activities row's config, as written there.
    watch: dict = field(default_factory=dict)


@workflow.defn(name="TenderWatchFlow")
class TenderWatchFlow:
    @workflow.run
    async def run(self, config: TenderWatchConfig) -> dict:
        return await workflow.execute_activity(
            "tender_watch_tick",
            config.watch,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=FAST,
        )
