"""Memory-reflection activity — nightly consolidation of per-agent memory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from temporalio import activity


@dataclass
class MemoryActivities:
    db_pool: Any

    @activity.defn
    async def prune_agent_memories(self, keep: int = 50) -> dict:
        """Cap each active agent's memory at `keep` rows (importance, then recency)."""
        from aegis.services.memory import prune_memories

        rows = await self.db_pool.fetch("SELECT id FROM agents WHERE active = TRUE")
        total = 0
        for r in rows:
            total += await prune_memories(self.db_pool, r["id"], keep)
        activity.logger.info("memory_pruned total=%s agents=%s keep=%s", total, len(rows), keep)
        return {"status": "ok", "pruned": total, "agents": len(rows)}
