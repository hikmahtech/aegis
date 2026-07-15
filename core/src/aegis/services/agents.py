"""Agent service — CRUD operations for the agents table."""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


async def list_agents(pool: asyncpg.Pool, active_only: bool = True) -> list[dict[str, Any]]:
    """List all agents, optionally filtered to active only."""
    query = "SELECT * FROM agents"
    if active_only:
        query += " WHERE active = TRUE"
    query += " ORDER BY id"
    rows = await pool.fetch(query)
    return [dict(r) for r in rows]


async def agents_by_tag(pool: asyncpg.Pool, tag: str) -> list[dict[str, Any]]:
    """Active agents whose capabilities contain the behavior tag, ordered by id."""
    rows = await pool.fetch(
        """
        SELECT * FROM agents
        WHERE active = TRUE AND capabilities @> jsonb_build_array($1::text)
        ORDER BY id
        """,
        tag,
    )
    return [dict(r) for r in rows]


async def resolve_tag(pool: asyncpg.Pool, tag: str) -> str | None:
    """Resolve a behavior tag to the single agent that owns it.

    Zero holders → None (callers skip the feature, mirroring the
    feature-flag skip pattern); multiple holders → deterministic first
    by id. Both anomalies are logged.
    """
    agents = await agents_by_tag(pool, tag)
    if not agents:
        logger.warning("no active agent holds behavior tag %r", tag)
        return None
    if len(agents) > 1:
        logger.warning(
            "behavior tag %r held by %s — using %r",
            tag,
            [a["id"] for a in agents],
            agents[0]["id"],
        )
    return agents[0]["id"]


async def warn_unknown_tool_refs(pool: asyncpg.Pool) -> None:
    """Boot-time check: warn on DB agents whose metadata.tool_set references a
    tool with no executor — that tool would silently never reach the agent.
    Never raises; user data must not brick startup.
    """
    from aegis.services.chat import TOOL_EXECUTORS

    for agent in await list_agents(pool):
        tool_set = (agent.get("metadata") or {}).get("tool_set") or []
        for name in sorted(set(tool_set) - set(TOOL_EXECUTORS)):
            logger.warning(
                "agent %r metadata.tool_set references unknown tool %r", agent["id"], name
            )


async def get_agent(pool: asyncpg.Pool, agent_id: str) -> dict[str, Any] | None:
    """Get a single agent by ID."""
    row = await pool.fetchrow("SELECT * FROM agents WHERE id = $1", agent_id)
    return dict(row) if row else None


async def create_agent(pool: asyncpg.Pool, data: dict[str, Any]) -> dict[str, Any]:
    """Insert a new agent. Requires id, name, role; sensible defaults for the rest.

    system_prompt_path is vestigial (the persona lives in agent_personalities —
    see aegis.services.personalities) but NOT NULL, so it defaults to ''.
    Raises asyncpg.UniqueViolationError if the id already exists — the route
    maps that to 409.
    """
    agent_id = (data.get("id") or "").strip()
    name = (data.get("name") or "").strip()
    role = (data.get("role") or "").strip()
    if not agent_id or not name or not role:
        raise ValueError("id, name and role are required")

    row = await pool.fetchrow(
        """
        INSERT INTO agents (
            id, name, role, system_prompt_path, capabilities, model_tier,
            metadata, active
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE)
        RETURNING *
        """,
        agent_id,
        name,
        role,
        data.get("system_prompt_path", ""),
        data.get("capabilities", []),
        data.get("model_tier", "balanced"),
        data.get("metadata", {}),
    )
    return dict(row)


async def update_agent(
    pool: asyncpg.Pool, agent_id: str, updates: dict[str, Any]
) -> dict[str, Any] | None:
    """Update an agent's mutable fields."""
    allowed = {
        "name",
        "role",
        "system_prompt_path",
        "capabilities",
        "active",
        "model_tier",
        "metadata",
    }
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        return await get_agent(pool, agent_id)

    set_clauses = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(filtered))
    values = [agent_id, *filtered.values()]
    row = await pool.fetchrow(f"UPDATE agents SET {set_clauses} WHERE id = $1 RETURNING *", *values)
    return dict(row) if row else None


async def reassign_agent_rows(pool: asyncpg.Pool, from_id: str, to_id: str) -> dict[str, int]:
    """Move every FK-owned row from one agent to another. Returns per-table counts.

    Tables are introspected from pg_constraint (FKs referencing agents(id) with
    NO ACTION) so future migrations can't rot a hardcoded list. CASCADE FKs
    (agent_personalities) are skipped — personas die with their agent rather
    than transfer. Runs in a single transaction.
    """
    fks = await pool.fetch(
        """
        SELECT con.conrelid::regclass::text AS table_name, att.attname AS column_name
        FROM pg_constraint con
        JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = ANY(con.conkey)
        WHERE con.contype = 'f'
          AND con.confrelid = 'public.agents'::regclass
          AND con.confdeltype = 'a'
        ORDER BY 1
        """
    )
    counts: dict[str, int] = {}
    async with pool.acquire() as conn, conn.transaction():
        for fk in fks:
            # identifiers come from pg_catalog, not user input — safe to interpolate
            result = await conn.execute(
                f"UPDATE {fk['table_name']} SET {fk['column_name']} = $2 "
                f"WHERE {fk['column_name']} = $1",
                from_id,
                to_id,
            )
            moved = int(result.split()[-1])
            if moved:
                counts[fk["table_name"]] = moved
    logger.info("agent_rows_reassigned from=%s to=%s counts=%s", from_id, to_id, counts)
    return counts


async def delete_agent(pool: asyncpg.Pool, agent_id: str) -> bool:
    """Delete an agent row. Returns False when the agent doesn't exist.

    Raises asyncpg.ForeignKeyViolationError while the agent still owns rows —
    callers surface that as "reassign first". agent_personalities cascades.
    """
    result = await pool.execute("DELETE FROM agents WHERE id = $1", agent_id)
    return result.split()[-1] == "1"
