"""Agent CRUD endpoints."""

from __future__ import annotations

from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request

from aegis.api.auth import verify_auth
from aegis.services.agents import create_agent as _create_agent
from aegis.services.agents import delete_agent as _delete_agent
from aegis.services.agents import get_agent as _get_agent
from aegis.services.agents import list_agents as _list_agents
from aegis.services.agents import reassign_agent_rows as _reassign_agent_rows
from aegis.services.agents import update_agent as _update_agent
from aegis.services.personalities import get_personality, set_personality

router = APIRouter(prefix="/api/agents", dependencies=[Depends(verify_auth)])

# Persona editor endpoints (admin UI) — separate prefix, same auth.
admin_router = APIRouter(prefix="/api/admin/agents", dependencies=[Depends(verify_auth)])


@admin_router.get("/{agent_id}/personality")
async def get_agent_personality(agent_id: str, request: Request) -> dict[str, str]:
    """The agent's persona — all four kinds (soul/agents/user/memory), DB-first."""
    pool = request.app.state.db_pool
    if not await _get_agent(pool, agent_id):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return await get_personality(pool, agent_id, use_cache=False)


@admin_router.put("/{agent_id}/personality")
async def put_agent_personality(
    agent_id: str, request: Request, body: dict[str, Any]
) -> dict[str, str]:
    """Upsert persona kinds. Body: any subset of {soul, agents, user, memory}.

    Returns the full updated persona (all four kinds).
    """
    pool = request.app.state.db_pool
    if not await _get_agent(pool, agent_id):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    try:
        return await set_personality(pool, agent_id, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("")
async def list_agents(request: Request, active: bool = True) -> list[dict[str, Any]]:
    """List all agents."""
    return await _list_agents(request.app.state.db_pool, active_only=active)


@router.get("/meta/options")
async def get_agent_options() -> dict[str, Any]:
    """Vocabulary for the admin Behavior tab: behavior tags, chat tools, tiers."""
    from aegis.agent_tags import BEHAVIOR_TAGS
    from aegis.services.chat import CHAT_TOOLS

    tools = []
    for spec in CHAT_TOOLS:
        fn = spec.get("function", {})
        if fn.get("name"):
            tools.append({"name": fn["name"], "description": fn.get("description", "")})
    return {
        "tags": [{"id": t, "description": d} for t, d in BEHAVIOR_TAGS.items()],
        "tools": sorted(tools, key=lambda t: t["name"]),
        "model_tiers": ["fast", "balanced", "smart"],
    }


def _validate_agent_patch(body: dict[str, Any]) -> None:
    """400 on malformed Behavior fields — a tool_set typo would otherwise be
    saved and silently never fire (issue #36 item 4)."""
    capabilities = body.get("capabilities")
    if capabilities is not None and (
        not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities)
    ):
        raise HTTPException(status_code=400, detail="capabilities must be a list of strings")

    metadata = body.get("metadata")
    if metadata is None:
        return
    if not isinstance(metadata, dict):
        raise HTTPException(status_code=400, detail="metadata must be an object")

    tool_set = metadata.get("tool_set")
    if tool_set is not None:
        if not isinstance(tool_set, list) or not all(isinstance(t, str) for t in tool_set):
            raise HTTPException(
                status_code=400, detail="metadata.tool_set must be a list of strings"
            )
        from aegis.services.chat import TOOL_EXECUTORS

        unknown = sorted(set(tool_set) - set(TOOL_EXECUTORS))
        if unknown:
            raise HTTPException(
                status_code=400, detail=f"unknown tools in tool_set: {', '.join(unknown)}"
            )

    async_dispatch = metadata.get("async_dispatch")
    if async_dispatch is not None and not isinstance(async_dispatch, bool):
        raise HTTPException(status_code=400, detail="metadata.async_dispatch must be a boolean")


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
    _validate_agent_patch(body)
    agent = await _update_agent(request.app.state.db_pool, agent_id, body)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return agent


@router.post("/{agent_id}/reassign")
async def reassign_agent(agent_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Move every FK-owned row (activities, runs, history, …) to another agent.

    Body: {"to": "<agent-id>"}. Run this before DELETE — the DB keeps
    RESTRICT FKs as the safety net, so a delete while rows remain is a 409.
    """
    to_id = str(body.get("to") or "").strip()
    if not to_id:
        raise HTTPException(status_code=400, detail="body.to (target agent id) is required")
    if to_id == agent_id:
        raise HTTPException(status_code=400, detail="target agent must differ from source")
    pool = request.app.state.db_pool
    for aid in (agent_id, to_id):
        if not await _get_agent(pool, aid):
            raise HTTPException(status_code=404, detail=f"Agent '{aid}' not found")
    counts = await _reassign_agent_rows(pool, agent_id, to_id)
    return {"reassigned": counts, "total": sum(counts.values())}


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: str, request: Request) -> None:
    """Delete an agent. Reserved 'system' agent is not deletable (#36)."""
    if agent_id == "system":
        raise HTTPException(status_code=400, detail="the 'system' agent is reserved")
    try:
        deleted = await _delete_agent(request.app.state.db_pool, agent_id)
    except asyncpg.ForeignKeyViolationError as e:
        raise HTTPException(
            status_code=409,
            detail=f"Agent '{agent_id}' still owns rows — reassign them first "
            f"(POST /api/agents/{agent_id}/reassign)",
        ) from e
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")


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
    tool_names = (
        (agent.get("metadata") or {}).get("tool_set") or AGENT_TOOL_SETS.get(agent_id) or []
    )

    descriptions: dict[str, str] = {}
    for spec in CHAT_TOOLS:
        fn = spec.get("function", {})
        name = fn.get("name")
        if name:
            descriptions[name] = fn.get("description", "")

    return [
        {"name": name, "description": descriptions.get(name, "")} for name in sorted(tool_names)
    ]
