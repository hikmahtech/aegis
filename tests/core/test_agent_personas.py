"""Personalities as DB config (OSS Phase B): persona-build DB-first + file
fallback, update_agent persona fields, file seed reader."""

from __future__ import annotations

import pytest_asyncio
from aegis.services.chat import _build_agent_system_prompt


def test_persona_db_first():
    out = _build_agent_system_prompt(
        "zz-no-such-agent",
        fallback="FB",
        persona={"soul": "I am Z", "operating_notes": "I do X", "user_context": "owner likes Y"},
    )
    assert "## Identity" in out and "I am Z" in out
    assert "## Operational Boundaries" in out and "I do X" in out
    assert "## User Context" in out and "owner likes Y" in out


def test_persona_empty_returns_fallback():
    out = _build_agent_system_prompt("zz-no-such-agent", fallback="FB", persona={})
    assert out == "FB"


def test_persona_partial_only_includes_present_sections():
    out = _build_agent_system_prompt("zz-no-such-agent", fallback="FB", persona={"soul": "only soul"})
    assert "## Identity" in out and "only soul" in out
    assert "## Operational Boundaries" not in out and "## User Context" not in out


def test_persona_tools_appended():
    out = _build_agent_system_prompt(
        "zz-no-such-agent", fallback="FB", persona={"soul": "S"}, tool_descriptions="tool docs"
    )
    assert "## Available Tools" in out and "tool docs" in out


def test_read_persona_files_reads_shipped_sebas():
    from aegis.seed import _read_persona_files

    soul, _ops, usr = _read_persona_files("sebas")
    # The shipped example personas include SOUL.md + USER.md; AGENTS.md
    # (operating notes) is intentionally omitted from the public examples.
    assert soul and usr


@pytest_asyncio.fixture(loop_scope="function")
async def temp_agent(db_pool):
    await db_pool.execute("DELETE FROM agents WHERE id = 'zzpersona'")
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, active) "
        "VALUES ('zzpersona', 'Z', 'r', '', true)"
    )
    yield db_pool
    await db_pool.execute("DELETE FROM agents WHERE id = 'zzpersona'")


async def test_update_agent_persona_fields(temp_agent):
    from aegis.services.agents import get_agent, update_agent

    await update_agent(
        temp_agent,
        "zzpersona",
        {"soul": "S", "operating_notes": "O", "user_context": "U", "model_tier": "smart"},
    )
    a = await get_agent(temp_agent, "zzpersona")
    assert a["soul"] == "S" and a["operating_notes"] == "O"
    assert a["user_context"] == "U" and a["model_tier"] == "smart"
