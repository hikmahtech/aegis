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
