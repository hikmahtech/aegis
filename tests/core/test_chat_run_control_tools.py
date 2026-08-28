"""dispatch_agent_run's task dedup, and the stop_agent_run executor."""

from __future__ import annotations

from aegis.services.chat import (
    CHAT_TOOLS,
    TOOL_EXECUTORS,
    ToolContext,
    _exec_dispatch_agent_run,
    _exec_stop_agent_run,
)
from temporalio.exceptions import WorkflowAlreadyStartedError


class _Temporal:
    def __init__(self, boom=None):
        self.started: list[dict] = []
        self._boom = boom

    async def start_workflow(self, name, arg, *, id, task_queue):
        if self._boom:
            raise self._boom
        self.started.append({"name": name, "arg": arg, "id": id})
        return object()


class _Connector:
    def __init__(self, result):
        self._result = result
        self.calls: list[str] = []

    async def stop_coding_run(self, run_id, host=""):
        self.calls.append(run_id)
        return self._result


# ── dispatch dedup ────────────────────────────────────────────────────────


async def test_a_task_bound_run_gets_a_deterministic_id():
    tc = _Temporal()
    ctx = ToolContext(agent_id="sebas", temporal_client=tc)
    out = await _exec_dispatch_agent_run(None, {"prompt": "do it", "todoist_task_id": "9x7"}, ctx)
    assert tc.started[0]["id"] == "agent-run-task-9x7"
    assert "agent-run-task-9x7" in out


async def test_an_untied_run_keeps_a_random_id():
    """Two 'look into X' asks are two legitimate runs, not a duplicate."""
    tc = _Temporal()
    ctx = ToolContext(agent_id="sebas", temporal_client=tc)
    await _exec_dispatch_agent_run(None, {"prompt": "do it"}, ctx)
    await _exec_dispatch_agent_run(None, {"prompt": "do it"}, ctx)
    ids = [c["id"] for c in tc.started]
    assert ids[0] != ids[1]
    assert all(i.startswith("agent-run-") and not i.startswith("agent-run-task-") for i in ids)


async def test_a_duplicate_task_dispatch_is_reported_not_crashed():
    tc = _Temporal(boom=WorkflowAlreadyStartedError("dup", "AgentRunFlow", run_id="r"))
    ctx = ToolContext(agent_id="sebas", temporal_client=tc)
    out = await _exec_dispatch_agent_run(None, {"prompt": "x", "todoist_task_id": "9x7"}, ctx)
    assert "already in flight" in out
    assert "stop_agent_run" in out


# ── stop ──────────────────────────────────────────────────────────────────


def test_stop_tool_is_registered_and_dispatchable():
    assert any(t["function"]["name"] == "stop_agent_run" for t in CHAT_TOOLS)
    assert TOOL_EXECUTORS["stop_agent_run"] is _exec_stop_agent_run


async def test_stop_reports_the_killed_window():
    conn = _Connector({"stopped": True, "reason": "", "window": "claude-bcp-aa11"})
    ctx = ToolContext(remote_script_connector=conn)
    out = await _exec_stop_agent_run(None, {"run_id": "aa11"}, ctx)
    assert "claude-bcp-aa11" in out
    assert conn.calls == ["aa11"]


async def test_stop_explains_a_missing_run_without_sounding_broken():
    conn = _Connector({"stopped": False, "reason": "not_found", "window": ""})
    ctx = ToolContext(remote_script_connector=conn)
    out = await _exec_stop_agent_run(None, {"run_id": "aa11"}, ctx)
    assert "finished already" in out


async def test_stop_requires_a_run_id():
    ctx = ToolContext(remote_script_connector=_Connector({}))
    out = await _exec_stop_agent_run(None, {}, ctx)
    assert "run_id is required" in out


async def test_stop_without_a_connector_says_so():
    out = await _exec_stop_agent_run(None, {"run_id": "aa11"}, ToolContext())
    assert "not configured" in out
