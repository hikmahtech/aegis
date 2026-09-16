"""Agent registry activities — resolve behavior tags to agent ids.

Groundwork for issue #36: flows call ``resolve_agents`` instead of hardcoding
seed agent ids (literal "maou"/"sebas", the ``_PANDORA`` constant). Semantics
intentionally mirror ``aegis.services.agents.resolve_tag`` on the core side;
kept self-contained here (own query, no core service import) because
activities own their DB access.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from temporalio import activity

from aegis_worker.shared.jsonb import decode_jsonb

logger = structlog.get_logger()


@dataclass
class AgentRegistryActivities:
    db_pool: Any

    @activity.defn
    async def resolve_agents(self, tags: list[str]) -> dict[str, str | None]:
        """Resolve each behavior tag to the active agent that declares it.

        For every requested tag: the id of the first ACTIVE agent (ORDER BY id)
        whose ``capabilities`` array contains the tag, else None. Callers treat
        None as "feature owner not configured" and skip, mirroring the
        feature-flag skip pattern in schedule_sync.
        """
        if not self.db_pool:
            return dict.fromkeys(tags)
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, capabilities FROM agents WHERE active = TRUE ORDER BY id"
            )
        resolved: dict[str, str | None] = {}
        for tag in tags:
            matches = [r["id"] for r in rows if tag in decode_jsonb(r["capabilities"], [])]
            if not matches:
                logger.warning("agent_tag_unresolved", tag=tag)
                resolved[tag] = None
            else:
                if len(matches) > 1:
                    logger.warning(
                        "agent_tag_ambiguous", tag=tag, winner=matches[0], candidates=matches
                    )
                resolved[tag] = matches[0]
        return resolved
