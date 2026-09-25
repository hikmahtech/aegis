"""WorldWatchFlow: one run of Raphael's world watch (#676). Everything happens in
the idempotent `world_watch_tick` activity; an item already filed is a no-op."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.shared.retry import FAST


@dataclass
class WorldWatchConfig:
    agent_id: str = "raphael"
    # The `world-watch-daily` activities row's config, as written there.
    watch: dict = field(default_factory=dict)


@workflow.defn(name="WorldWatchFlow")
class WorldWatchFlow:
    @workflow.run
    async def run(self, config: WorldWatchConfig) -> dict:
        return await workflow.execute_activity(
            "world_watch_tick",
            config.watch,
            start_to_close_timeout=timedelta(minutes=3),
            retry_policy=FAST,
        )
