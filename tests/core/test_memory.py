"""Tests for the per-agent memory learning loop (Phase 4)."""

from __future__ import annotations

import pytest_asyncio
from aegis.services.memory import (
    format_memories,
    prune_memories,
    recent_memories,
    record_correction_from_interaction,
    record_memory,
)

_AID = "zzmem-agent"


@pytest_asyncio.fixture(loop_scope="function")
async def mem_agent(db_pool):
    await db_pool.execute("DELETE FROM agent_memory WHERE agent_id = $1", _AID)
    await db_pool.execute("DELETE FROM agents WHERE id = $1", _AID)
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, active) "
        "VALUES ($1, 'Z', 'tester', '', true)",
        _AID,
    )
    yield db_pool
    await db_pool.execute("DELETE FROM agent_memory WHERE agent_id = $1", _AID)
    await db_pool.execute("DELETE FROM agents WHERE id = $1", _AID)


async def test_record_and_recent(mem_agent):
    await record_memory(mem_agent, _AID, "low note", importance=0.2)
    await record_memory(mem_agent, _AID, "high lesson", importance=0.9)
    out = await recent_memories(mem_agent, _AID, limit=5)
    assert out[0] == "high lesson"  # importance-ordered
    assert "low note" in out


async def test_record_empty_is_noop(mem_agent):
    await record_memory(mem_agent, _AID, "   ")
    assert await recent_memories(mem_agent, _AID) == []


async def test_correction_from_interaction_records_with_reason(mem_agent):
    await record_correction_from_interaction(
        mem_agent, _AID, "Open a PR on the alert?", {"value": "reject", "reason": "wrong base branch"}
    )
    out = await recent_memories(mem_agent, _AID)
    assert len(out) == 1
    assert "wrong base branch" in out[0] and "reject" in out[0]


async def test_correction_without_reason_is_noop(mem_agent):
    await record_correction_from_interaction(mem_agent, _AID, "Approve?", {"value": "accept"})
    assert await recent_memories(mem_agent, _AID) == []


async def test_prune_caps_to_keep(mem_agent):
    for i in range(10):
        await record_memory(mem_agent, _AID, f"mem {i}", importance=0.5)
    deleted = await prune_memories(mem_agent, _AID, keep=3)
    assert deleted == 7
    assert len(await recent_memories(mem_agent, _AID, limit=50)) == 3


def test_format_memories():
    assert format_memories([]) == ""
    s = format_memories(["a", "b"])
    assert "What you've learned" in s and "- a" in s and "- b" in s
