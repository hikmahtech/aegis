"""Agent service — CRUD operations for the agents table."""

from __future__ import annotations

from typing import Any

import asyncpg


async def list_agents(pool: asyncpg.Pool, active_only: bool = True) -> list[dict[str, Any]]:
    """List all agents, optionally filtered to active only."""
    query = "SELECT * FROM agents"
    if active_only:
        query += " WHERE active = TRUE"
    query += " ORDER BY id"
    rows = await pool.fetch(query)
    return [dict(r) for r in rows]


async def get_agent(pool: asyncpg.Pool, agent_id: str) -> dict[str, Any] | None:
    """Get a single agent by ID."""
    row = await pool.fetchrow("SELECT * FROM agents WHERE id = $1", agent_id)
    return dict(row) if row else None


async def update_agent(
    pool: asyncpg.Pool, agent_id: str, updates: dict[str, Any]
) -> dict[str, Any] | None:
    """Update an agent's mutable fields."""
    allowed = {
        "name",
        "role",
        "description",
        "system_prompt_path",
        "avatar_url",
        "capabilities",
        "active",
        "model_tier",
        "soul",
        "operating_notes",
        "user_context",
    }
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        return await get_agent(pool, agent_id)

    set_clauses = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(filtered))
    values = [agent_id, *filtered.values()]
    row = await pool.fetchrow(f"UPDATE agents SET {set_clauses} WHERE id = $1 RETURNING *", *values)
    return dict(row) if row else None
