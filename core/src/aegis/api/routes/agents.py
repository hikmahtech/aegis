"""Agent CRUD endpoints."""

from __future__ import annotations

from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request

from aegis.api.auth import verify_auth
from aegis.services.agents import create_agent as _create_agent
from aegis.services.agents import get_agent as _get_agent
from aegis.services.agents import list_agents as _list_agents
from aegis.services.agents import update_agent as _update_agent

router = APIRouter(prefix="/api/agents", dependencies=[Depends(verify_auth)])


@router.get("")
async def list_agents(request: Request, active: bool = True) -> list[dict[str, Any]]:
    """List all agents."""
    return await _list_agents(request.app.state.db_pool, active_only=active)


@router.post("", status_code=201)
async def create_agent(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Create a new agent. Body: {id, name, role, model_tier?, capabilities?, metadata?}."""
    try:
        return await _create_agent(request.app.state.db_pool, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except asyncpg.UniqueViolationError as e:
        raise HTTPException(
            status_code=409, detail=f"Agent '{body.get('id')}' already exists"
        ) from e


@router.get("/{agent_id}")
async def get_agent(agent_id: str, request: Request) -> dict[str, Any]:
    """Get a single agent by ID."""
    agent = await _get_agent(request.app.state.db_pool, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return agent


@router.patch("/{agent_id}")
async def update_agent(agent_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Update an agent's configuration."""
    agent = await _update_agent(request.app.state.db_pool, agent_id, body)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return agent


_DRAFT_PROMPT = """You are helping configure an AI agent persona for a personal \
assistant platform. The agent's name is "{name}" and its role is "{role}".

The user describes the agent they want:
"{description}"

Write the persona as STRICT JSON with exactly these three string fields, each a \
few short markdown paragraphs:
{{"soul": "<identity — who this agent is, its voice, principles, communication style>",
  "operating_notes": "<operational boundaries — what it does and does not do, how it uses tools, domain limits>",
  "user_context": "<what the agent should assume about its user; keep generic unless the description specifies otherwise>"}}

Reply with ONLY the JSON object."""


@router.post("/{agent_id}/draft")
async def draft_persona(agent_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Draft a persona with the configured LLM from a one-line description.

    Returns {soul, operating_notes, user_context} for the user to review and
    edit before saving — never writes the agent itself.
    """
    description = (body.get("description") or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="description is required")
    llm = getattr(request.app.state, "llm", None)
    if llm is None:
        raise HTTPException(status_code=503, detail="LLM backend not configured")

    pool = request.app.state.db_pool
    agent = await _get_agent(pool, agent_id) or {}
    from aegis.llm import parse_llm_json, resolve_model_for_agent

    model = await resolve_model_for_agent(pool, agent_id)
    prompt = _DRAFT_PROMPT.format(
        name=agent.get("name", agent_id), role=agent.get("role", ""), description=description
    )
    result = await llm.think(prompt, model=model, max_tokens=2000, purpose="persona_draft")
    raw = result.get("response", "") if isinstance(result, dict) else str(result)
    parsed = parse_llm_json(raw) or {}
    return {
        "soul": str(parsed.get("soul", "")),
        "operating_notes": str(parsed.get("operating_notes", "")),
        "user_context": str(parsed.get("user_context", "")),
    }


@router.get("/{agent_id}/tools")
async def get_agent_tools(agent_id: str, request: Request) -> list[dict[str, str]]:
    """Return the tool set this agent has access to, joined with tool descriptions.

    Prefers the agent's DB metadata.tool_set (so UI-created agents work), falling
    back to the hardcoded AGENT_TOOL_SETS — mirrors chat.py's _get_agent_tools.
    """
    from aegis.services.chat import AGENT_TOOL_SETS, CHAT_TOOLS

    agent = await _get_agent(request.app.state.db_pool, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    tool_names = (agent.get("metadata") or {}).get("tool_set") or AGENT_TOOL_SETS.get(agent_id) or []

    descriptions: dict[str, str] = {}
    for spec in CHAT_TOOLS:
        fn = spec.get("function", {})
        name = fn.get("name")
        if name:
            descriptions[name] = fn.get("description", "")

    return [
        {"name": name, "description": descriptions.get(name, "")} for name in sorted(tool_names)
    ]
