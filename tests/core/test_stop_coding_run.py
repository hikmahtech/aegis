"""RemoteScriptConnector.stop_coding_run — kill a run's tmux window by run id.

SSH is stubbed at `_exec`, so these assert the command shape and the failure
policy rather than talking to a host.
"""

from __future__ import annotations

from aegis.connectors.remote_script import RemoteScriptConnector

WINDOWS = (
    "@1:zsh\n"
    "@7:claude-bcp-aa11bb22\n"
    "@8:kimi-homelab-gitops-cc33dd44\n"
)


def _connector():
    conn = RemoteScriptConnector(host="codehost", user="u", key_file="/dev/null")
    conn._host = "codehost"
    conn._tmux_session = "remote"
    return conn


def _stub(conn, results):
    issued = []

    async def fake_exec(host, remote_cmd, timeout, **kwargs):
        issued.append(remote_cmd)
        return results.pop(0)

    async def noop_refresh():
        return None

    conn._exec = fake_exec
    conn._refresh_config = noop_refresh
    return issued


def _ok(stdout=""):
    return {"status": "succeeded", "exit_code": 0, "stdout": stdout, "stderr": ""}


def _fail(stderr="ssh: connect failed"):
    return {"status": "failed", "exit_code": -1, "stdout": "", "stderr": stderr}


async def test_kills_the_matching_window():
    conn = _connector()
    issued = _stub(conn, [_ok(WINDOWS), _ok()])
    result = await conn.stop_coding_run("aa11bb22")
    assert result == {"stopped": True, "reason": "", "window": "claude-bcp-aa11bb22"}
    assert "kill-window" in issued[1]
    assert "@7" in issued[1]


async def test_matches_a_kimi_window_too():
    conn = _connector()
    issued = _stub(conn, [_ok(WINDOWS), _ok()])
    result = await conn.stop_coding_run("cc33dd44")
    assert result["stopped"] is True
    assert result["window"] == "kimi-homelab-gitops-cc33dd44"
    assert "@8" in issued[1]


async def test_unknown_run_is_not_found_not_an_error():
    """A finished run, or one launched detached past the tmux cap."""
    conn = _connector()
    issued = _stub(conn, [_ok(WINDOWS)])
    result = await conn.stop_coding_run("deadbeef")
    assert result["stopped"] is False
    assert result["reason"] == "not_found"
    assert len(issued) == 1, "must not issue a kill when nothing matched"


async def test_never_kills_a_window_without_an_agent_prefix():
    """Only `kimi-`/`claude-` windows are agent runs.

    A user's own window could end with the same suffix; killing it because the
    id matched would take out their shell, so the prefix filter has to run
    before the suffix match.
    """
    conn = _connector()
    issued = _stub(conn, [_ok("@1:my-notes-aa11bb22\n")])
    result = await conn.stop_coding_run("aa11bb22")
    assert result["stopped"] is False
    assert result["reason"] == "not_found"
    assert len(issued) == 1, "listed, but must not have issued a kill"


async def test_suffix_match_is_anchored():
    """`bb22` is a substring of `aa11bb22` but not its run id."""
    conn = _connector()
    _stub(conn, [_ok(WINDOWS)])
    result = await conn.stop_coding_run("bb22")
    assert result["stopped"] is False
    assert result["reason"] == "not_found"


async def test_a_hostile_run_id_is_refused_before_any_ssh():
    """The value is spliced into a remote command — validate, never quote-and-hope."""
    conn = _connector()
    issued = _stub(conn, [])
    for bad in ["a; rm -rf /", "$(whoami)", "`id`", "a b", "..", "x" * 200, ""]:
        result = await conn.stop_coding_run(bad)
        assert result["stopped"] is False, bad
        assert result["reason"] == "invalid_run_id", bad
    assert issued == [], "a rejected run id must not reach the host"


async def test_tmux_unreachable_is_reported_not_raised():
    conn = _connector()
    _stub(conn, [_fail()])
    result = await conn.stop_coding_run("aa11bb22")
    assert result == {"stopped": False, "reason": "tmux_unreachable", "window": ""}


async def test_a_failed_kill_is_reported():
    conn = _connector()
    _stub(conn, [_ok(WINDOWS), _fail("no such window")])
    result = await conn.stop_coding_run("aa11bb22")
    assert result["stopped"] is False
    assert result["reason"] == "kill_failed"
    assert result["window"] == "claude-bcp-aa11bb22"
