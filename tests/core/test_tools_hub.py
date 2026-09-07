"""The `set_service_state` chat tool: a thin, honest wrapper over the hub.

A registered tool is called the way the chat loop calls it: `(pool, args, ctx)`.
"""

from __future__ import annotations

import uuid

import pytest
from aegis.services.chat import TOOL_EXECUTORS
from aegis.services.hub import list_service_states
from aegis.services.tools.base import ToolContext
from aegis.services.tools.hub import _exec_set_service_state

pytestmark = pytest.mark.asyncio

CTX = ToolContext(agent_id="pandoras-actor")


async def _call(pool, **args) -> str:
    return await _exec_set_service_state(pool, args, CTX)


async def test_tool_is_registered_and_dispatches_to_the_same_function():
    assert TOOL_EXECUTORS["set_service_state"] is _exec_set_service_state


async def test_sets_a_window_and_lists_what_is_in_force(db_pool):
    s = f"svc_{uuid.uuid4().hex[:8]}"
    out = await _call(db_pool, subject=s, state="deploying", minutes=20, note="rolling core")
    assert out.startswith(f"{s}: deploying until ")
    assert "set by chat:pandoras-actor" in out
    assert f"- {s} (service): deploying until" in out
    rows = [r for r in await list_service_states(db_pool) if r["subject"] == s]
    assert rows and rows[0]["note"] == "rolling core"


async def test_ok_clears_and_says_so(db_pool):
    s = f"svc_{uuid.uuid4().hex[:8]}"
    await _call(db_pool, subject=s, state="maintenance", minutes=5)
    out = await _call(db_pool, subject=s, state="ok")
    assert out.startswith(f"{s}: window cleared.")
    out = await _call(db_pool, subject=s, state="ok")
    assert out.startswith(f"{s}: no window was set.")


async def test_star_is_a_global_window(db_pool):
    try:
        out = await _call(db_pool, subject="*", state="maintenance", minutes=1, note="power cut")
        assert "- * (*): maintenance until" in out
    finally:
        await _call(db_pool, subject="*", state="ok")


async def test_empty_subject_is_refused(db_pool):
    out = await _call(db_pool, subject="  ", state="deploying")
    assert out.startswith("Refused: subject is required")


async def test_unknown_args_are_dropped_not_fatal(db_pool):
    s = f"svc_{uuid.uuid4().hex[:8]}"
    out = await _call(db_pool, subject=s, state="degraded", bogus=1)
    assert out.startswith(f"{s}: degraded until ")
    await _call(db_pool, subject=s, state="ok")
