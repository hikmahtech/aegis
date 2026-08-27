"""RemoteScriptConnector.list_coding_sessions — enumeration and fail-open.

SSH is stubbed at `_exec`, so these assert the command shape and the failure
policy rather than talking to a host.
"""

from __future__ import annotations

from aegis.connectors.remote_script import RemoteScriptConnector

BASE = "/home/u/Workspace"

ONE_BUSY = """[
  {"pid": 1, "cwd": "/home/u/Workspace/acme/api", "kind": "interactive",
   "sessionId": "s-1", "name": "api-2d", "status": "busy"}
]"""


def _connector(**overrides):
    """A connector wired to explicit config, with SSH stubbed by the caller."""
    conn = RemoteScriptConnector(host="codehost", user="u", key_file="/dev/null")
    conn._host = "codehost"
    conn._repo_base = BASE
    conn._claude_binary = "/usr/local/bin/claude"
    conn._claude_config_dirs = {"personal": "/home/u/.claude-personal"}
    conn._inventory_config = {"enabled": True, "skip_when_busy": True, "accounts": []}
    conn._inventory_config.update(overrides)
    return conn


def _stub_exec(conn, results):
    """Replace _exec and _refresh_config; record the commands issued."""
    issued = []

    async def fake_exec(host, remote_cmd, timeout, **kwargs):
        issued.append(remote_cmd)
        return results.pop(0)

    async def noop_refresh():
        return None

    conn._exec = fake_exec
    conn._refresh_config = noop_refresh
    return issued


def _ok(stdout):
    return {"status": "succeeded", "exit_code": 0, "stdout": stdout, "stderr": ""}


def _fail(stderr="ssh: connect failed"):
    return {"status": "failed", "exit_code": -1, "stdout": "", "stderr": stderr}


async def test_disabled_makes_no_ssh_call():
    conn = _connector(enabled=False)
    issued = _stub_exec(conn, [])
    result = await conn.list_coding_sessions()
    assert result["status"] == "disabled"
    assert result["sessions"] == []
    assert issued == []


async def test_enumerates_each_account_and_normalises():
    conn = _connector()
    conn._claude_config_dirs = {"personal": "/home/u/.cp", "work": "/home/u/.cw"}
    issued = _stub_exec(conn, [_ok(ONE_BUSY), _ok("[]")])
    result = await conn.list_coding_sessions()
    assert result["status"] == "ok"
    assert len(issued) == 2
    assert "CLAUDE_CONFIG_DIR=/home/u/.cp" in issued[0]
    assert "agents --json" in issued[0]
    assert result["sessions"][0]["repo"] == "acme/api"
    assert result["sessions"][0]["account"] == "personal"


async def test_accounts_allow_list_limits_enumeration():
    conn = _connector(accounts=["work"])
    conn._claude_config_dirs = {"personal": "/home/u/.cp", "work": "/home/u/.cw"}
    issued = _stub_exec(conn, [_ok("[]")])
    await conn.list_coding_sessions()
    assert len(issued) == 1
    assert "/home/u/.cw" in issued[0]


async def test_one_account_failing_does_not_lose_the_other():
    conn = _connector()
    conn._claude_config_dirs = {"personal": "/home/u/.cp", "work": "/home/u/.cw"}
    _stub_exec(conn, [_fail(), _ok(ONE_BUSY)])
    result = await conn.list_coding_sessions()
    assert result["status"] == "ok"
    assert len(result["sessions"]) == 1
    assert result["errors"][0]["account"] == "personal"
    # Assert the SSH stderr specifically, not merely "an error happened". A
    # failed _exec also yields empty stdout, so the parse branch would report
    # its own error and this test would pass with the SSH branch deleted.
    assert "ssh: connect failed" in result["errors"][0]["error"]


async def test_all_accounts_failing_is_unavailable():
    conn = _connector()
    _stub_exec(conn, [_fail()])
    result = await conn.list_coding_sessions()
    assert result["status"] == "unavailable"
    assert result["sessions"] == []
    assert "ssh: connect failed" in result["errors"][0]["error"]


async def test_unparseable_output_is_an_error_not_an_exception():
    conn = _connector()
    _stub_exec(conn, [_ok("segfault, sorry")])
    result = await conn.list_coding_sessions()
    assert result["status"] == "unavailable"
    assert "no JSON array" in result["errors"][0]["error"]


async def test_no_claude_binary_is_unavailable_not_a_crash():
    conn = _connector()
    conn._claude_binary = ""
    issued = _stub_exec(conn, [])
    result = await conn.list_coding_sessions()
    assert result["status"] == "unavailable"
    assert issued == []


async def test_config_dir_is_shell_quoted():
    conn = _connector()
    conn._claude_config_dirs = {"odd": "/home/u/dir with space"}
    issued = _stub_exec(conn, [_ok("[]")])
    await conn.list_coding_sessions()
    assert "'/home/u/dir with space'" in issued[0]
