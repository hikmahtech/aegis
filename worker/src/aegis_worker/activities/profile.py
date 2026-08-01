"""Profile activities — the auditable persona write path, for flows.

Thin wrappers over aegis.services.personalities so a flow never imports the
FastAPI layer. No LLM calls, no business logic: deciding *what* to write is the
calling flow's job (A2/A5/A7), this module only reads and commits.

Registered in worker/__main__.py but intentionally dormant — no flow, no
schedule, no _ACTIVITY_TYPE_MAP entry yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from temporalio import activity


@dataclass
class ProfileActivities:
    db_pool: Any

    @activity.defn
    async def read_profile_context(self, agent_id: str) -> dict[str, str]:
        """The agent's four persona kinds (soul/agents/user/memory), DB-first.

        Cache-bypassing: a flow that is about to patch the profile must reason
        about what is committed now, not what a 30s TTL remembers.
        """
        from aegis.services.personalities import get_personality

        return await get_personality(self.db_pool, agent_id, use_cache=False)

    @activity.defn
    async def apply_profile_patch(self, payload: dict) -> dict:
        """Patch one persona kind and log a revision.

        Payload: ``{agent_id, kind?, new_content, source?, interaction_id?,
        allow_shrink?}`` — `kind` defaults to the user-context doc, which is
        the only one automated writers should be touching.
        """
        from aegis.services.personalities import apply_profile_patch as _apply

        result = await _apply(
            self.db_pool,
            payload["agent_id"],
            payload.get("kind") or "user",
            payload.get("new_content") or "",
            source=payload.get("source") or "worker",
            interaction_id=payload.get("interaction_id"),
            allow_shrink=bool(payload.get("allow_shrink")),
        )
        activity.logger.info(
            "profile_patch_applied agent=%s kind=%s revision=%s source=%s %s->%s chars",
            result["agent_id"],
            result["kind"],
            result["revision_id"],
            result["source"],
            result["before_length"],
            result["after_length"],
        )
        return result
