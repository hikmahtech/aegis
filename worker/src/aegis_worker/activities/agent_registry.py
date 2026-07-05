"""Agent registry activities — resolve behavior tags to agent ids.

Groundwork for issue #36: flows call ``resolve_agents`` instead of hardcoding
seed agent ids (literal "maou"/"sebas", the ``_PANDORA`` constant). Semantics
intentionally mirror ``aegis.services.agents.resolve_tag`` on the core side;
kept self-contained here (own query, no core service import) because
activities own their DB access.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import structlog
from temporalio import activity

logger = structlog.get_logger()


def _decode_capabilities(raw) -> list:
    """Normalise the jsonb capabilities column to a plain list.

    With the JSONB codec registered (aegis.db.create_pool) asyncpg returns a
    Python list; without it the raw value is a JSON string — same dual-path
    handling as channels._decode_config.
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    return json.loads(raw)


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
            matches = [r["id"] for r in rows if tag in _decode_capabilities(r["capabilities"])]
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
