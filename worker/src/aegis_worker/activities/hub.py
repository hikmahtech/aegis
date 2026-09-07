"""Worker-side activities over the problem hub (`aegis.services.hub`).

Thin by design: the logic lives in core's `services/hub.py`, which both
packages import, so a workflow reaches the hub through these and never carries
SQL of its own.
"""

from __future__ import annotations

import asyncpg
from aegis.services import hub
from temporalio import activity


class HubActivities:
    def __init__(self, db_pool: asyncpg.Pool | None) -> None:
        self.db_pool = db_pool

    @activity.defn
    async def promote_expired_suppressions(self) -> dict:
        """Open every `suppressed` problem whose deploy/maintenance window has
        passed without a resolution. Run by `HubSweepFlow`."""
        if self.db_pool is None:
            return {"promoted": 0, "problem_ids": []}
        ids = await hub.promote_expired_suppressions(self.db_pool)
        return {"promoted": len(ids), "problem_ids": ids}

    @activity.defn
    async def clear_converged_deploys(self, stuck_services: list[str]) -> dict:
        """End `deploying` windows for services the heartbeat sees converged.
        `stuck_services` is the heartbeat's current below-desired list."""
        if self.db_pool is None:
            return {"cleared": []}
        cleared = await hub.clear_converged_deploys(self.db_pool, list(stuck_services or []))
        return {"cleared": cleared}
