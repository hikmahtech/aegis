"""The read-only list_coding_sessions chat tool.

A schema without an executor is advertised to the model but can never run; an
executor without a schema is never offered. Both halves are asserted because
`chat.py` has no auto-discovery — each is hand-registered.
"""

from __future__ import annotations

from aegis.services.chat import (
    CHAT_TOOLS,
    TOOL_EXECUTORS,
    ToolContext,
    _exec_list_coding_sessions,
)

TOOL = "list_coding_sessions"

ONE_SESSION = {
    "account": "personal",
    "name": "api-2d",
    "repo": "acme/api",
    "status": "busy",
    "owner": "human",
    "kind": "interactive",
    "session_id": "s-1",
    "cwd": "/w/acme/api",
}


class _Connector:
    def __init__(self, result):
        self._result = result

    async def list_coding_sessions(self):
        return self._result


def _ctx(result):
    return ToolContext(remote_script_connector=_Connector(result))


def test_tool_is_registered_and_dispatchable():
    assert any(t["function"]["name"] == TOOL for t in CHAT_TOOLS)
    assert TOOL in TOOL_EXECUTORS
    assert TOOL_EXECUTORS[TOOL] is _exec_list_coding_sessions


def test_tool_takes_no_arguments():
    schema = next(t["function"] for t in CHAT_TOOLS if t["function"]["name"] == TOOL)
    assert schema["parameters"]["required"] == []


async def test_reports_sessions():
    ctx = _ctx(
        {"status": "ok", "sessions": [ONE_SESSION], "errors": [], "skip_when_busy": True}
    )
    out = await _exec_list_coding_sessions(None, {}, ctx)
    assert "api-2d" in out
    assert "acme/api" in out
    assert "human" in out
    # The private socket path must never reach a chat transcript.
    assert "cc-socks" not in out


async def test_disabled_says_so_rather_than_looking_empty():
    ctx = _ctx({"status": "disabled", "sessions": [], "errors": [], "skip_when_busy": True})
    out = await _exec_list_coding_sessions(None, {}, ctx)
    assert "disabled" in out.lower()


async def test_unavailable_is_distinct_from_empty():
    ctx = _ctx(
        {
            "status": "unavailable",
            "sessions": [],
            "errors": [{"account": "personal", "error": "ssh: connect failed"}],
            "skip_when_busy": True,
        }
    )
    out = await _exec_list_coding_sessions(None, {}, ctx)
    assert "Could not read" in out
    assert "ssh: connect failed" in out


async def test_empty_but_ok_reads_as_nothing_running():
    ctx = _ctx({"status": "ok", "sessions": [], "errors": [], "skip_when_busy": True})
    out = await _exec_list_coding_sessions(None, {}, ctx)
    assert "No coding sessions" in out


async def test_no_connector_is_reported():
    out = await _exec_list_coding_sessions(None, {}, ToolContext())
    assert "not configured" in out
