"""Unit tests for the new Kimi CLI methods on RemoteScriptConnector.

We mock asyncio.create_subprocess_exec to capture the SSH command shape.
Integration against real node-a is out of scope for unit tests.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog.testing
from aegis.api.routes.mcp_server import (
    AGENT_RUN_APPROVAL_TIMEOUT_S,
    APPROVAL_TOOL_NAME,
)
from aegis.connectors.remote_script import (
    MCP_TOOL_TIMEOUT_MS,
    PERMISSION_PROMPT_TOOL,
    RemoteScriptConnector,
    _agent_launch_flags,
    _mcp_config_path,
    _mcp_run_config,
    _plan_tmux_launch,
    _sanitize_window_repo,
)


@pytest.fixture
def conn():
    return RemoteScriptConnector(
        host="node-a",
        user="user",
        key_file="/tmp/fake_key",
        repo_base="/home/user/Workspace",
    )


@pytest.mark.asyncio
async def test_start_kimi_run_happy_path(conn):
    """Verify the launched command contains the expected kimi flags."""
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        mock_exec.return_value = proc
        result = await conn.start_kimi_run(
            repo="youruser/aegis",
            prompt="investigate failing test",
            kimi_binary="/home/user/.local/bin/kimi",
        )
    assert result["status"] == "running"
    assert result["repo"] == "youruser/aegis"
    # Happy path: worktree succeeded — worktree_path must be non-empty.
    assert result["worktree_path"] != ""
    assert "-aegis-wt/" in result["worktree_path"]
    # Combined text across all subprocess invocations must mention kimi + flags.
    combined = " ".join(" ".join(str(a) for a in c.args) for c in mock_exec.call_args_list)
    assert "/home/user/.local/bin/kimi" in combined
    assert "--output-format stream-json" in combined
    # kimi CLI 0.31.x dropped --print/--input-format/--work-dir (issue #271);
    # the prompt is substituted into -p via $(cat ...) instead of stdin/flags.
    assert "--work-dir" not in combined
    assert "--print" not in combined
    assert "--input-format" not in combined
    assert '-p "$(cat' in combined
    assert "/home/user/Workspace/youruser/aegis" in combined


@pytest.mark.asyncio
async def test_start_kimi_run_repo_missing_returns_failed(conn):
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 1  # test -d fails
        mock_exec.return_value = proc
        result = await conn.start_kimi_run(
            repo="youruser/missing",
            prompt="x",
            kimi_binary="/home/user/.local/bin/kimi",
        )
    assert result["status"] == "failed"
    assert "checkout missing" in result["error"].lower()
    # engine (item 4, #275): the kimi->claude fallback in the flow keys on
    # this field, so a launch failure that never reaches an engine-aware
    # caller must still carry it.
    assert result["engine"] == "kimi"
    # No JIT clone: a missing checkout must never trigger a `git clone`.
    combined = " ".join(" ".join(str(a) for a in c.args) for c in mock_exec.call_args_list)
    assert "git clone" not in combined


def _stub_proc(*, returncode: int | None = None, communicate_side_effect=None) -> AsyncMock:
    """Build a subprocess-like mock with .kill() and .wait() observable."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(side_effect=communicate_side_effect or [(b"", b"")])
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=returncode if returncode is not None else 0)
    return proc


@pytest.mark.asyncio
async def test_run_script_timeout_reaps_subprocess(conn):
    """When run_script times out, the subprocess is killed and waited on."""
    proc = _stub_proc(returncode=None, communicate_side_effect=TimeoutError())

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        result = await conn.run_script("infra/list", [], timeout=1)

    assert result["status"] == "timed_out"
    proc.kill.assert_called_once()
    proc.wait.assert_awaited()


@pytest.mark.asyncio
async def test_run_script_cancellation_reaps_subprocess(conn):
    """asyncio.CancelledError during communicate() must kill+wait the child before re-raising."""
    proc = _stub_proc(returncode=None, communicate_side_effect=asyncio.CancelledError())

    with (
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        pytest.raises(asyncio.CancelledError),
    ):
        await conn.run_script("infra/list", [], timeout=10)

    proc.kill.assert_called_once()
    proc.wait.assert_awaited()


@pytest.mark.asyncio
async def test_fetch_kimi_run_output_cancellation_reaps_subprocess(conn):
    """fetch_kimi_run_output cancellations must not leak the cat subprocess."""
    proc = _stub_proc(returncode=None, communicate_side_effect=asyncio.CancelledError())

    with (
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        pytest.raises(asyncio.CancelledError),
    ):
        await conn.fetch_kimi_run_output("/tmp/aegis-kimi-run-x.jsonl")

    proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_start_kimi_run_check_timeout_reaps_subprocess(conn):
    """If the directory-check ssh hangs and gets killed, the proc is reaped."""
    proc = _stub_proc(returncode=None, communicate_side_effect=TimeoutError())

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        result = await conn.start_kimi_run(
            repo="youruser/aegis",
            prompt="x",
            kimi_binary="/home/user/.local/bin/kimi",
        )

    # Failure path returns a dict — but the proc must still have been reaped.
    assert result["status"] == "failed"
    proc.kill.assert_called_once()


def _make_proc_sequence(returncodes: list[int]) -> list[AsyncMock]:
    """Build a list of subprocess mocks, one per create_subprocess_exec call."""
    procs = []
    for rc in returncodes:
        p = AsyncMock()
        p.returncode = rc
        p.communicate = AsyncMock(return_value=(b"", b""))
        p.kill = MagicMock()
        p.wait = AsyncMock(return_value=rc)
        procs.append(p)
    return procs


@pytest.mark.asyncio
async def test_start_kimi_run_creates_worktree_and_runs_kimi_there(conn):
    """When worktree add succeeds, kimi should run inside the worktree path.

    Call sequence (repo exists, worktree succeeds):
      0: test -d check (rc=0)
      1: git pull (rc=0)
      2: mkdir + git worktree add --detach (rc=0)
      3: cat > prompt_file (rc=0)
      4: kimi launch (rc=0)
    """
    procs = _make_proc_sequence([0, 0, 0, 0, 0])
    with patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec:
        result = await conn.start_kimi_run(
            repo="youruser/bcp",
            prompt="investigate bug",
            kimi_binary="/usr/local/bin/kimi",
        )

    assert result["status"] == "running"
    assert result["worktree_path"] != ""
    assert "-aegis-wt/" in result["worktree_path"]

    all_cmds = [" ".join(str(a) for a in c.args) for c in mock_exec.call_args_list]

    # One command must perform the worktree add
    assert any("worktree add --detach" in cmd and "-aegis-wt/" in cmd for cmd in all_cmds)

    # The kimi launch (last command) must cd into the worktree, NOT bare repo_path.
    # kimi CLI 0.31.x has no --work-dir flag; the `cd` prefix carries the cwd.
    kimi_cmd = all_cmds[-1]
    repo_path = "/home/user/Workspace/youruser/bcp"
    assert "--work-dir" not in kimi_cmd
    # The cd target must reference the worktree path (contains -aegis-wt/)
    assert "-aegis-wt/" in kimi_cmd
    assert result["repo_path"] == repo_path


@pytest.mark.asyncio
async def test_start_kimi_run_worktree_failure_falls_back_to_shared(conn):
    """When worktree add fails (rc=1), kimi should run in the shared clone.

    Call sequence:
      0: test -d check (rc=0)
      1: git pull (rc=0)
      2: mkdir + git worktree add --detach (rc=1 — FAIL)
      3: cat > prompt_file (rc=0)
      4: kimi launch (rc=0)
    """
    procs = _make_proc_sequence([0, 0, 1, 0, 0])
    with patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec:
        result = await conn.start_kimi_run(
            repo="youruser/bcp",
            prompt="investigate bug",
            kimi_binary="/usr/local/bin/kimi",
        )

    assert result["status"] == "running"
    # Fallback: worktree_path must be empty string
    assert result["worktree_path"] == ""

    repo_path = "/home/user/Workspace/youruser/bcp"
    all_cmds = [" ".join(str(a) for a in c.args) for c in mock_exec.call_args_list]
    kimi_cmd = all_cmds[-1]

    # kimi must run from the shared repo_path (not a worktree); the `cd` prefix
    # carries cwd since kimi CLI 0.31.x has no --work-dir flag.
    assert "--work-dir" not in kimi_cmd
    assert repo_path in kimi_cmd
    assert "-aegis-wt/" not in kimi_cmd


@pytest.mark.asyncio
async def test_start_kimi_run_prompt_write_nonzero_rc_fails_before_launch(conn):
    """A nonzero (non-ssh-error) rc from `cat > prompt_file` means the prompt
    is missing/truncated on the remote — with kimi's `-p "$(cat ...)"` form
    that would otherwise launch a full-auto agent with an EMPTY prompt.
    Before the fix, only exit_code == -1 (an ssh-level error) failed here, so
    a remote rc=1 (e.g. disk full, permission denied — ssh itself succeeded)
    launched anyway. Falsifiability: reverting the `!= 0` guard back to
    `== -1` makes this test launch (status flips to 'running' and a 5th
    subprocess call — the kimi launch — appears).

    Call sequence (repo exists, worktree succeeds):
      0: test -d check (rc=0)
      1: git pull (rc=0)
      2: mkdir + git worktree add --detach (rc=0)
      3: cat > prompt_file (rc=1 — remote failure, NOT an ssh error)
    """
    procs = _make_proc_sequence([0, 0, 0, 1])
    with patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec:
        result = await conn.start_kimi_run(
            repo="youruser/bcp",
            prompt="investigate bug",
            kimi_binary="/usr/local/bin/kimi",
        )

    assert result["status"] == "failed"
    assert result["engine"] == "kimi"
    # Exactly 4 subprocess calls — the launch (a 5th) must never happen.
    assert mock_exec.call_count == 4


@pytest.mark.asyncio
async def test_start_kimi_run_launch_ssh_failure_carries_engine(conn):
    """A genuine ssh/connect failure at launch (`_exec` status='failed' —
    nothing was launched remotely) is fallback-eligible: engine is carried
    through unchanged so the flow's kimi->claude fallback can retry.

    Call sequence (repo exists, worktree succeeds):
      0: test -d check (rc=0)
      1: git pull (rc=0)
      2: mkdir + git worktree add --detach (rc=0)
      3: cat > prompt_file (rc=0)
      4: nohup launch — ssh itself fails (status='failed', not 'timed_out')
    """
    procs = [
        _stub_proc(returncode=0),
        _stub_proc(returncode=0),
        _stub_proc(returncode=0),
        _stub_proc(returncode=0),
    ]
    launch_proc = AsyncMock()
    launch_proc.communicate = AsyncMock(side_effect=OSError("ssh connection refused"))
    launch_proc.returncode = None
    launch_proc.kill = MagicMock()
    launch_proc.wait = AsyncMock(return_value=None)
    procs.append(launch_proc)

    with patch("asyncio.create_subprocess_exec", side_effect=procs):
        result = await conn.start_kimi_run(
            repo="youruser/bcp",
            prompt="investigate bug",
            kimi_binary="/usr/local/bin/kimi",
        )

    assert result["status"] == "failed"
    assert result["engine"] == "kimi"


@pytest.mark.asyncio
async def test_start_kimi_run_launch_timeout_engine_empty_not_fallback_eligible(conn):
    """A launch that TIMES OUT (`_exec` status='timed_out') may have already
    forked the detached `(nohup ... &)` remotely before the 15s ssh timeout
    hit — the kimi agent could be ALIVE. engine must be '' so the flow's
    kimi->claude fallback does not race a possibly-live agent on the same
    deterministic fix branch. Before this fix, `engine` was carried through
    unconditionally on any exit_code == -1, including this timed-out case
    (falsifiability: reverting `"" if launch["status"] == "timed_out" else
    engine` back to a bare `engine` makes this test see engine == "kimi").

    Call sequence identical to the ssh-failure test above, except the launch
    step raises TimeoutError instead of a generic connection error.
    """
    procs = [
        _stub_proc(returncode=0),
        _stub_proc(returncode=0),
        _stub_proc(returncode=0),
        _stub_proc(returncode=0),
        _stub_proc(returncode=None, communicate_side_effect=TimeoutError()),
    ]

    with patch("asyncio.create_subprocess_exec", side_effect=procs):
        result = await conn.start_kimi_run(
            repo="youruser/bcp",
            prompt="investigate bug",
            kimi_binary="/usr/local/bin/kimi",
        )

    assert result["status"] == "failed"
    assert result["engine"] == ""


@pytest.mark.asyncio
async def test_remove_worktree_issues_git_worktree_remove(conn):
    """remove_worktree should run `git worktree remove --force` and not raise on nonzero exit."""
    worktree = "/home/user/Workspace/bcp-aegis-wt/run123"
    proc = AsyncMock()
    proc.returncode = 1  # nonzero — must not raise
    proc.communicate = AsyncMock(return_value=(b"", b"error: worktree not found"))
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=1)

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec:
        # Must not raise even with nonzero exit
        await conn.remove_worktree(worktree)

    assert mock_exec.called
    cmd = " ".join(str(a) for a in mock_exec.call_args.args)
    assert "worktree remove --force" in cmd
    assert worktree in cmd


@pytest.mark.asyncio
async def test_remove_worktree_noop_on_empty_path(conn):
    """remove_worktree must return immediately without spawning any process when path is empty."""
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        await conn.remove_worktree("")

    mock_exec.assert_not_called()


@pytest.fixture
def conn_b():
    """Connector with a preferred kimi_host so the probe path is exercised."""
    return RemoteScriptConnector(
        host="node-a",
        user="user",
        key_file="/tmp/fake_key",
        repo_base="/home/user/Workspace",
        kimi_host="node-b",
        tmux_session="remote",
        tmux_window_cap=10,
    )


def test_ssh_args_host_targets_explicit_host(conn):
    args = conn._ssh_args_host("node-b", "true")
    assert "user@node-b" in args
    # default host helper still targets the configured host
    assert "user@node-a" in conn._ssh_args("true")


@pytest.mark.asyncio
async def test_probe_host_uses_fast_batch_ssh_and_returns_true_on_rc0(conn_b):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.returncode = 0
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec:
        ok = await conn_b._probe_host("node-b")
    assert ok is True
    argv = " ".join(str(a) for a in mock_exec.call_args.args)
    assert "ConnectTimeout=3" in argv
    assert "BatchMode=yes" in argv
    assert "user@node-b" in argv


@pytest.mark.asyncio
async def test_probe_host_returns_false_on_nonzero(conn_b):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.returncode = 255
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=255)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        ok = await conn_b._probe_host("node-b")
    assert ok is False


@pytest.mark.asyncio
async def test_resolve_kimi_host_prefers_reachable_kimi_host(conn_b):
    with patch.object(conn_b, "_probe_host", AsyncMock(return_value=True)):
        host, use_tmux = await conn_b._resolve_kimi_host()
    assert host == "node-b"
    assert use_tmux is True


@pytest.mark.asyncio
async def test_resolve_kimi_host_falls_back_when_probe_fails(conn_b):
    with patch.object(conn_b, "_probe_host", AsyncMock(return_value=False)):
        host, use_tmux = await conn_b._resolve_kimi_host()
    assert host == "node-a"
    assert use_tmux is False


@pytest.mark.asyncio
async def test_resolve_kimi_host_unset_never_probes(conn):
    # conn has no kimi_host → resolve must not probe and must use the base host
    with patch.object(conn, "_probe_host", AsyncMock(return_value=True)) as probe:
        host, use_tmux = await conn._resolve_kimi_host()
    probe.assert_not_called()
    assert host == "node-a"
    assert use_tmux is False


def test_sanitize_window_repo_takes_basename_and_strips_unsafe():
    assert _sanitize_window_repo("youruser/bcp") == "bcp"
    assert _sanitize_window_repo("acme/error-pages") == "error-pages"
    assert _sanitize_window_repo("weird/na me!@#") == "na-me---"
    assert _sanitize_window_repo("") == "repo"


def _wl(*rows: tuple[str, str, str]) -> str:
    # rows of (window_id, window_name, pane_dead)
    return "\n".join(f"{wid}:{name}:{dead}" for wid, name, dead in rows)


def test_plan_under_cap_launches_without_pruning():
    out = _wl(("@0", "bash", "0"), ("@1", "kimi-bcp-aaaa", "0"))
    prune, use_tmux = _plan_tmux_launch(out, cap=10)
    assert prune == []
    assert use_tmux is True


def test_plan_ignores_non_kimi_windows_for_the_count():
    # 10 non-kimi windows + 0 kimi → far under cap
    rows = [(f"@{i}", "bash", "0") for i in range(10)]
    prune, use_tmux = _plan_tmux_launch(_wl(*rows), cap=10)
    assert prune == []
    assert use_tmux is True


def test_plan_at_cap_prunes_oldest_dead_window():
    rows = [(f"@{i}", f"kimi-bcp-{i:04d}", "1" if i in (2, 5) else "0") for i in range(10)]
    prune, use_tmux = _plan_tmux_launch(_wl(*rows), cap=10)
    # need = 10 - 10 + 1 = 1 → kill the oldest dead window (@2)
    assert prune == ["@2"]
    assert use_tmux is True


def test_plan_all_live_skips_tmux():
    rows = [(f"@{i}", f"kimi-bcp-{i:04d}", "0") for i in range(10)]
    prune, use_tmux = _plan_tmux_launch(_wl(*rows), cap=10)
    assert prune == []
    assert use_tmux is False


def test_plan_over_cap_prunes_enough_dead_oldest_first():
    # 12 kimi windows, cap 10, dead = @3,@7,@9 → need 12-10+1 = 3 → all three, oldest first
    rows = [(f"@{i}", f"kimi-bcp-{i:04d}", "1" if i in (3, 7, 9) else "0") for i in range(12)]
    prune, use_tmux = _plan_tmux_launch(_wl(*rows), cap=10)
    assert prune == ["@3", "@7", "@9"]
    assert use_tmux is True


def test_plan_over_cap_insufficient_dead_skips_tmux():
    # 12 kimi windows, only 1 dead but need 3 → cannot fit without killing live → skip
    rows = [(f"@{i}", f"kimi-bcp-{i:04d}", "1" if i == 4 else "0") for i in range(12)]
    prune, use_tmux = _plan_tmux_launch(_wl(*rows), cap=10)
    assert prune == []
    assert use_tmux is False


@pytest.mark.asyncio
async def test_start_kimi_run_unset_kimi_host_uses_nohup_on_base_host(conn):
    """Default connector (no kimi_host) keeps today's nohup launch + host=node-a."""
    procs = _make_proc_sequence([0, 0, 0, 0, 0])  # test-d, pull, worktree, prompt, launch
    with patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec:
        result = await conn.start_kimi_run(
            repo="youruser/bcp", prompt="x", kimi_binary="/usr/local/bin/kimi"
        )
    assert result["status"] == "running"
    assert result["host"] == "node-a"
    assert result["in_tmux"] is False
    all_cmds = [" ".join(str(a) for a in c.args) for c in mock_exec.call_args_list]
    assert any("nohup" in c and "tee" not in c for c in all_cmds)
    assert all("tmux" not in c for c in all_cmds)
    # every ssh targeted the base host
    assert all("user@node-b" not in c for c in all_cmds)


@pytest.mark.asyncio
async def test_start_kimi_run_tmux_mode_launches_window_with_tee(conn_b):
    """kimi_host reachable + under cap → tmux new-window with tee + remain-on-exit on node-b."""
    # _probe_host is patched (no subprocess), so the ssh calls are:
    #   0 test-d, 1 pull, 2 worktree, 3 prompt, 4 ensure+list, 5 new-window launch
    procs = _make_proc_sequence([0, 0, 0, 0, 0, 0])
    procs[4].communicate = AsyncMock(return_value=(b"@0:bash:0\n", b""))  # list-windows output
    with (
        patch.object(conn_b, "_probe_host", AsyncMock(return_value=True)),
        patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec,
    ):
        result = await conn_b.start_kimi_run(
            repo="youruser/bcp", prompt="investigate", kimi_binary="/usr/local/bin/kimi"
        )
    assert result["status"] == "running"
    assert result["host"] == "node-b"
    assert result["in_tmux"] is True
    all_cmds = [" ".join(str(a) for a in c.args) for c in mock_exec.call_args_list]
    # the kimi work ssh all targeted node-b
    assert any("user@node-b" in c for c in all_cmds)
    launch = all_cmds[-1]
    assert "tmux new-window" in launch
    assert "kimi-bcp-" in launch
    assert "| tee" in launch
    assert "remain-on-exit on" in launch
    assert "--output-format stream-json" in launch
    # output file still the canonical /tmp path the flow polls
    assert "/tmp/aegis-kimi-run-" in launch


@pytest.mark.asyncio
async def test_start_kimi_run_tmux_all_live_falls_back_to_detached(conn_b):
    """kimi_host reachable but all 10 windows live → detached nohup on node-b, in_tmux False."""
    live = "\n".join(f"@{i}:kimi-bcp-{i:04d}:0" for i in range(10))
    # _probe_host patched; ssh calls: 0 test-d, 1 pull, 2 worktree, 3 prompt,
    #   4 ensure+list (all live → skip tmux), 5 detached nohup fallback
    procs = _make_proc_sequence([0, 0, 0, 0, 0, 0])
    procs[4].communicate = AsyncMock(return_value=(live.encode(), b""))
    with (
        patch.object(conn_b, "_probe_host", AsyncMock(return_value=True)),
        patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec,
    ):
        result = await conn_b.start_kimi_run(
            repo="youruser/bcp", prompt="investigate", kimi_binary="/usr/local/bin/kimi"
        )
    assert result["status"] == "running"
    assert result["host"] == "node-b"
    assert result["in_tmux"] is False
    last = " ".join(str(a) for a in mock_exec.call_args_list[-1].args)
    assert "nohup" in last
    assert "tmux new-window" not in last
    assert "user@node-b" in last


@pytest.mark.asyncio
async def test_start_kimi_run_probe_fail_uses_base_nohup(conn_b):
    """kimi_host set but unreachable → node-a nohup, host=node-a, no probe-driven tmux."""
    procs = _make_proc_sequence([0, 0, 0, 0, 0])
    with (
        patch.object(conn_b, "_probe_host", AsyncMock(return_value=False)),
        patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec,
    ):
        result = await conn_b.start_kimi_run(
            repo="youruser/bcp", prompt="x", kimi_binary="/usr/local/bin/kimi"
        )
    assert result["host"] == "node-a"
    assert result["in_tmux"] is False
    all_cmds = [" ".join(str(a) for a in c.args) for c in mock_exec.call_args_list]
    assert all("tmux" not in c for c in all_cmds)
    assert all("user@node-b" not in c for c in all_cmds)


@pytest.mark.asyncio
async def test_fetch_kimi_run_output_targets_explicit_host(conn):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b'{"x":1}\n', b""))
    proc.returncode = 0
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec:
        out = await conn.fetch_kimi_run_output("/tmp/aegis-kimi-run-x.jsonl", host="node-b")
    assert out is not None
    argv = " ".join(str(a) for a in mock_exec.call_args.args)
    assert "user@node-b" in argv


@pytest.mark.asyncio
async def test_fetch_kimi_run_output_defaults_to_base_host(conn):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b'{"x":1}\n', b""))
    proc.returncode = 0
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec:
        await conn.fetch_kimi_run_output("/tmp/aegis-kimi-run-x.jsonl")
    argv = " ".join(str(a) for a in mock_exec.call_args.args)
    assert "user@node-a" in argv
    assert "user@node-b" not in argv


@pytest.mark.asyncio
async def test_remove_worktree_targets_explicit_host(conn):
    worktree = "/home/user/Workspace/bcp-aegis-wt/run123"
    proc = AsyncMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec:
        await conn.remove_worktree(worktree, host="node-b")
    argv = " ".join(str(a) for a in mock_exec.call_args.args)
    assert "user@node-b" in argv
    assert "worktree remove --force" in argv


# ── claude-engine routing (org repos pinned to base host on the claude CLI) ──


@pytest.fixture
def conn_claude():
    """Connector with both a preferred kimi_host AND claude-org routing."""
    return RemoteScriptConnector(
        host="node-a",
        user="user",
        key_file="/tmp/fake_key",
        repo_base="/home/user/Workspace",
        kimi_host="node-b",
        tmux_session="remote",
        tmux_window_cap=10,
        claude_orgs="acme",
        claude_binary="/home/user/.local/bin/claude",
    )


def test_engine_for_matches_org_case_insensitively(conn_claude):
    assert conn_claude._engine_for("Acme/bcp") == "claude"
    assert conn_claude._engine_for("acme/Spy") == "claude"
    assert conn_claude._engine_for("youruser/aegis") == "kimi"
    assert conn_claude._engine_for("") == "kimi"
    # org-less value never routes to claude
    assert conn_claude._engine_for("bcp") == "kimi"


def test_engine_for_unset_orgs_always_kimi(conn_b):
    assert conn_b._engine_for("Acme/bcp") == "kimi"


@pytest.mark.asyncio
async def test_start_kimi_run_claude_org_pins_base_host_in_tmux(conn_claude):
    """Org repo → claude CLI on the base host (never probes / never uses node-b)."""
    # ssh calls: 0 test-d, 1 pull, 2 worktree, 3 prompt, 4 ensure+list, 5 new-window
    procs = _make_proc_sequence([0, 0, 0, 0, 0, 0])
    procs[4].communicate = AsyncMock(return_value=(b"@0:bash:0\n", b""))
    with (
        patch.object(conn_claude, "_probe_host", AsyncMock(return_value=True)) as probe,
        patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec,
    ):
        result = await conn_claude.start_kimi_run(
            repo="bcp",
            prompt="investigate",
            kimi_binary="/home/user/.local/bin/kimi",
            github_repo="Acme/bcp",
        )
    probe.assert_not_called()
    assert result["status"] == "running"
    assert result["host"] == "node-a"
    assert result["engine"] == "claude"
    assert result["in_tmux"] is True
    all_cmds = [" ".join(str(a) for a in c.args) for c in mock_exec.call_args_list]
    assert all("user@node-b" not in c for c in all_cmds)
    launch = all_cmds[-1]
    assert "tmux new-window" in launch
    assert "claude-bcp-" in launch
    assert "/home/user/.local/bin/claude" in launch
    assert "--dangerously-skip-permissions" in launch
    assert "--verbose" in launch
    assert "--output-format stream-json" in launch
    # claude has no --work-dir flag; cwd comes from the cd
    assert "--work-dir" not in launch
    assert "| tee" in launch
    assert "/tmp/aegis-kimi-run-" in launch


def test_agent_launch_flags_claude_config_dir():
    # claude + config_dir → CLAUDE_CONFIG_DIR prefix (quoted), before the binary
    flags = _agent_launch_flags("claude", "/bin/claude", "/w", "/p", "/home/user/.config/aegis")
    assert flags.startswith("CLAUDE_CONFIG_DIR=/home/user/.config/aegis /bin/claude")
    # claude without config_dir → no env prefix (default ~/.claude)
    assert "CLAUDE_CONFIG_DIR" not in _agent_launch_flags("claude", "/bin/claude", "/w", "/p")
    # kimi never gets the env, even if a config_dir is passed
    assert "CLAUDE_CONFIG_DIR" not in _agent_launch_flags("kimi", "/bin/kimi", "/w", "/p", "/x")


def test_agent_launch_flags_claude_unchanged():
    """The claude branch is untouched by the kimi CLI 0.31.x fix (issue #271) —
    still --print/--verbose/--dangerously-skip-permissions with the prompt
    piped in over stdin."""
    flags = _agent_launch_flags("claude", "/bin/claude", "/w", "/p")
    assert flags == (
        "/bin/claude --print --output-format stream-json "
        "--verbose --dangerously-skip-permissions < /p"
    )


def test_agent_launch_flags_kimi_cli_031_form():
    """kimi CLI 0.31.1 (issue #271) dropped --print/--input-format/--work-dir
    outright, and rejects both --auto and --yolo in prompt mode ("Cannot
    combine --prompt with --auto/--yolo" — confirmed by running the real
    0.31.1 binary; -p mode is unconditionally forced to full-auto permission
    internally, per the shipped CLI source, so no permission flag is passed
    at all). The prompt is substituted into -p via `$(cat <prompt_file>)`
    instead of a stdin redirect."""
    flags = _agent_launch_flags("kimi", "/bin/kimi", "/w", "/p")
    assert flags == '/bin/kimi --output-format stream-json -p "$(cat /p)"'
    # Old CLI (<0.31) flags must be gone.
    assert "--print" not in flags
    assert "--input-format" not in flags
    assert "--work-dir" not in flags
    # Both rejected-in-prompt-mode permission flags must be absent.
    assert "--auto" not in flags
    assert "--yolo" not in flags
    assert " -y" not in flags
    # No trailing stdin redirect (kimi no longer reads the prompt from stdin).
    assert not flags.endswith(f"< {'/p'}")
    assert "< /p" not in flags


def test_agent_launch_flags_kimi_prompt_file_shlex_quoted():
    """The prompt_file path is shlex-quoted INSIDE the $(cat ...) substitution
    (e.g. a path containing a space must not break the command)."""
    flags = _agent_launch_flags("kimi", "/bin/kimi", "/w", "/tmp/a b/prompt.txt")
    assert "$(cat '/tmp/a b/prompt.txt')" in flags


@pytest.mark.asyncio
async def test_engine_override_forces_personal_claude_on_base_host(conn_claude):
    """Non-org repo + engine_override='claude' → claude CLI on node-a (not node-b)
    with CLAUDE_CONFIG_DIR pointing at the personal login (the kimi→claude
    fallback path)."""
    procs = _make_proc_sequence([0, 0, 0, 0, 0, 0])
    procs[4].communicate = AsyncMock(return_value=(b"@0:bash:0\n", b""))
    with (
        patch.object(conn_claude, "_probe_host", AsyncMock(return_value=True)) as probe,
        patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec,
    ):
        result = await conn_claude.start_kimi_run(
            repo="aegis",
            prompt="investigate",
            kimi_binary="/home/user/.local/bin/kimi",
            github_repo="youruser/aegis",  # NON-org → would normally be kimi
            engine_override="claude",
            claude_config_dir="/home/user/.config/aegis",
        )
    probe.assert_not_called()  # claude pins base host, never probes node-b
    assert result["engine"] == "claude"
    assert result["host"] == "node-a"
    all_cmds = [" ".join(str(a) for a in c.args) for c in mock_exec.call_args_list]
    assert all("user@node-b" not in c for c in all_cmds)
    launch = all_cmds[-1]
    assert "CLAUDE_CONFIG_DIR=/home/user/.config/aegis" in launch
    assert "/home/user/.local/bin/claude" in launch


@pytest.mark.asyncio
async def test_claude_account_selects_resource_config_dir(conn_claude):
    """Resource-scoped routing: `claude_account` resolves to that account's
    CLAUDE_CONFIG_DIR and wins over org routing; an explicit claude_config_dir
    still wins over the account."""
    conn_claude._claude_config_dirs = {"work": "/cfg/work", "personal": "/cfg/personal"}

    # account label → its config dir (repo would otherwise be kimi; force claude)
    procs = _make_proc_sequence([0, 0, 0, 0, 0, 0])
    procs[4].communicate = AsyncMock(return_value=(b"@0:bash:0\n", b""))
    with (
        patch.object(conn_claude, "_probe_host", AsyncMock(return_value=True)),
        patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec,
    ):
        await conn_claude.start_kimi_run(
            repo="aegis", prompt="x", kimi_binary="/k",
            github_repo="youruser/aegis", engine_override="claude",
            claude_account="work",
        )
    launch = " ".join(str(a) for a in mock_exec.call_args_list[-1].args)
    assert "CLAUDE_CONFIG_DIR=/cfg/work" in launch

    # explicit claude_config_dir beats the account
    procs2 = _make_proc_sequence([0, 0, 0, 0, 0, 0])
    procs2[4].communicate = AsyncMock(return_value=(b"@0:bash:0\n", b""))
    with (
        patch.object(conn_claude, "_probe_host", AsyncMock(return_value=True)),
        patch("asyncio.create_subprocess_exec", side_effect=procs2) as mock_exec2,
    ):
        await conn_claude.start_kimi_run(
            repo="aegis", prompt="x", kimi_binary="/k",
            github_repo="youruser/aegis", engine_override="claude",
            claude_account="work", claude_config_dir="/explicit",
        )
    launch2 = " ".join(str(a) for a in mock_exec2.call_args_list[-1].args)
    assert "CLAUDE_CONFIG_DIR=/explicit" in launch2


@pytest.mark.asyncio
async def test_start_kimi_run_claude_tmux_all_live_falls_back_to_nohup_on_base(conn_claude):
    live = "\n".join(f"@{i}:claude-bcp-{i:04d}:0" for i in range(10))
    procs = _make_proc_sequence([0, 0, 0, 0, 0, 0])
    procs[4].communicate = AsyncMock(return_value=(live.encode(), b""))
    with patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec:
        result = await conn_claude.start_kimi_run(
            repo="bcp",
            prompt="investigate",
            kimi_binary="/home/user/.local/bin/kimi",
            github_repo="Acme/bcp",
        )
    assert result["status"] == "running"
    assert result["host"] == "node-a"
    assert result["engine"] == "claude"
    assert result["in_tmux"] is False
    last = " ".join(str(a) for a in mock_exec.call_args_list[-1].args)
    assert "nohup" in last
    assert "/home/user/.local/bin/claude" in last
    assert "tmux new-window" not in last


@pytest.mark.asyncio
async def test_start_kimi_run_non_org_repo_still_prefers_kimi_host(conn_claude):
    """A personal repo on the claude-routing connector keeps the kimi/node-b path."""
    procs = _make_proc_sequence([0, 0, 0, 0, 0, 0])
    procs[4].communicate = AsyncMock(return_value=(b"@0:bash:0\n", b""))
    with (
        patch.object(conn_claude, "_probe_host", AsyncMock(return_value=True)) as probe,
        patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec,
    ):
        result = await conn_claude.start_kimi_run(
            repo="aegis",
            prompt="investigate",
            kimi_binary="/home/user/.local/bin/kimi",
            github_repo="youruser/aegis",
        )
    probe.assert_called_once()
    assert result["host"] == "node-b"
    assert result["engine"] == "kimi"
    launch = " ".join(str(a) for a in mock_exec.call_args_list[-1].args)
    assert "kimi-aegis-" in launch
    assert "/home/user/.local/bin/kimi" in launch
    assert "--work-dir" not in launch
    assert '-p "$(cat' in launch
    assert "--dangerously-skip-permissions" not in launch


@pytest.mark.asyncio
async def test_start_kimi_run_org_repo_without_claude_orgs_stays_kimi(conn_b):
    """Dormant default: no claude_orgs configured → org repos keep today's kimi path."""
    procs = _make_proc_sequence([0, 0, 0, 0, 0, 0])
    procs[4].communicate = AsyncMock(return_value=(b"@0:bash:0\n", b""))
    with (
        patch.object(conn_b, "_probe_host", AsyncMock(return_value=True)),
        patch("asyncio.create_subprocess_exec", side_effect=procs),
    ):
        result = await conn_b.start_kimi_run(
            repo="bcp",
            prompt="x",
            kimi_binary="/usr/local/bin/kimi",
            github_repo="Acme/bcp",
        )
    assert result["engine"] == "kimi"
    assert result["host"] == "node-b"


@pytest.mark.asyncio
async def test_start_kimi_run_claude_binary_missing_fails_fast(conn_b):
    """claude routing selected but no claude binary configured → explicit failure."""
    conn = RemoteScriptConnector(
        host="node-a",
        user="user",
        key_file="/tmp/fake_key",
        repo_base="/home/user/Workspace",
        claude_orgs="acme",
        claude_binary="",
    )
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        result = await conn.start_kimi_run(
            repo="bcp",
            prompt="x",
            kimi_binary="/usr/local/bin/kimi",
            github_repo="Acme/bcp",
        )
    mock_exec.assert_not_called()
    assert result["status"] == "failed"
    assert "claude_binary" in result["error"]
    # This IS the claude path (engine resolved to claude before the binary
    # check) — hardcoded, not resolved via engine_override.
    assert result["engine"] == "claude"


def test_plan_counts_claude_windows_toward_cap():
    rows = [(f"@{i}", f"claude-bcp-{i:04d}", "0") for i in range(10)]
    prune, use_tmux = _plan_tmux_launch(_wl(*rows), cap=10)
    assert prune == []
    assert use_tmux is False


def test_plan_mixed_kimi_and_claude_windows_prunes_oldest_dead():
    rows = [
        ("@0", "kimi-bcp-0000", "1"),
        ("@1", "claude-bcp-0001", "0"),
        ("@2", "kimi-aegis-0002", "0"),
        ("@3", "claude-spy-0003", "1"),
    ]
    prune, use_tmux = _plan_tmux_launch(_wl(*rows), cap=4)
    assert prune == ["@0"]
    assert use_tmux is True


@pytest.mark.asyncio
async def test_fetch_kimi_run_output_drops_truncated_leading_fragment(conn):
    """A capped read starts mid-line; that JSON debris must not survive.

    Left in, it reaches the assessor LLM looking like the agent's own prose
    (prod: every kimi verdict came back confidence=0.0 with `{"role":"tool"…`
    inside root_cause).
    """
    from aegis.connectors.remote_script import _KIMI_OUTPUT_CAP

    fragment = 'timeout": 30}"}}]}'
    body = '{"role":"assistant","content":[{"type":"text","text":"done"}]}'
    filler = "\n".join([body] * 200)
    raw = fragment + "\n" + filler
    raw += "x" * max(0, _KIMI_OUTPUT_CAP - len(raw))  # force the cap to bite

    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(raw.encode(), b""))
    proc.returncode = 0
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        out = await conn.fetch_kimi_run_output("/tmp/aegis-kimi-run-x.jsonl")

    assert out is not None
    assert not out.startswith(fragment)
    assert body in out


@pytest.mark.asyncio
async def test_fetch_kimi_run_output_keeps_first_line_when_under_cap(conn):
    """An uncapped read is complete — never drop its first line."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b'{"session_id":"s1"}\nhello\n', b""))
    proc.returncode = 0
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        out = await conn.fetch_kimi_run_output("/tmp/aegis-kimi-run-x.jsonl")

    assert out is not None
    assert out.startswith('{"session_id":"s1"}')


# ── kimi_run_alive (issue #271 fail-fast probe) ──────────────────────────


@pytest.mark.asyncio
async def test_kimi_run_alive_rc0_file_held_open_returns_true(conn):
    """fuser rc=0 means some process still holds output_file open → alive."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.returncode = 0
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec:
        alive = await conn.kimi_run_alive("/tmp/aegis-kimi-run-x.jsonl")
    assert alive is True
    argv = " ".join(str(a) for a in mock_exec.call_args.args)
    assert "fuser" in argv
    assert "/tmp/aegis-kimi-run-x.jsonl" in argv


@pytest.mark.asyncio
async def test_kimi_run_alive_rc1_not_held_returns_false(conn):
    """fuser rc=1 means nobody holds the file (or it was never created) → dead."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.returncode = 1
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=1)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        alive = await conn.kimi_run_alive("/tmp/aegis-kimi-run-x.jsonl")
    assert alive is False


@pytest.mark.asyncio
async def test_kimi_run_alive_unexpected_rc_fails_open(conn):
    """A surprising fuser exit code (e.g. missing binary → rc 127) must never
    be mistaken for 'dead' — fail open so a flaky probe can't kill a healthy
    run. It must also be logged (item 2, #275) so a permanently-inconclusive
    probe (e.g. a host missing `fuser`) is visible instead of a silent
    permanent no-op."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"", b"fuser: command not found"))
    proc.returncode = 127
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=127)
    with (
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        structlog.testing.capture_logs() as logs,
    ):
        alive = await conn.kimi_run_alive("/tmp/aegis-kimi-run-x.jsonl")
    assert alive is True
    warnings = [log for log in logs if log["event"] == "kimi_run_alive_probe_inconclusive"]
    assert len(warnings) == 1
    assert warnings[0]["exit_code"] == 127
    assert warnings[0]["output_file"] == "/tmp/aegis-kimi-run-x.jsonl"
    assert warnings[0]["host"] == "node-a"


@pytest.mark.asyncio
async def test_kimi_run_alive_rc0_does_not_log_inconclusive_warning(conn):
    """Falsifiability control: a normal rc=0 (alive) probe must NOT emit the
    inconclusive-probe warning — proves the log in the test above is keyed on
    the unexpected rc, not emitted unconditionally."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.returncode = 0
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    with (
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        structlog.testing.capture_logs() as logs,
    ):
        await conn.kimi_run_alive("/tmp/aegis-kimi-run-x.jsonl")
    assert not [log for log in logs if log["event"] == "kimi_run_alive_probe_inconclusive"]


@pytest.mark.asyncio
async def test_kimi_run_alive_ssh_error_fails_open(conn):
    """An SSH/exec-level failure (exit_code=-1) must fail open, not report dead."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(side_effect=OSError("ssh connection refused"))
    proc.returncode = None
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=None)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        alive = await conn.kimi_run_alive("/tmp/aegis-kimi-run-x.jsonl")
    assert alive is True


@pytest.mark.asyncio
async def test_kimi_run_alive_targets_explicit_host(conn):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.returncode = 0
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec:
        await conn.kimi_run_alive("/tmp/aegis-kimi-run-x.jsonl", host="node-b")
    argv = " ".join(str(a) for a in mock_exec.call_args.args)
    assert "user@node-b" in argv


@pytest.mark.asyncio
async def test_kimi_run_alive_defaults_to_base_host(conn):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.returncode = 0
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec:
        await conn.kimi_run_alive("/tmp/aegis-kimi-run-x.jsonl")
    argv = " ".join(str(a) for a in mock_exec.call_args.args)
    assert "user@node-a" in argv
    assert "user@node-b" not in argv


# ── workspace mount: SKILL.md runbooks + the AEGIS MCP tool server ──────────
#
# Both are claude-only and both are best-effort: a missing skills dir or a
# failed MCP-config write must degrade the run, never fail the launch.


@pytest.fixture
def conn_mount():
    """Claude-routed connector with everything the mount needs configured."""
    return RemoteScriptConnector(
        host="node-a",
        user="user",
        key_file="/tmp/fake_key",
        repo_base="/home/user/Workspace",
        claude_orgs="acme",
        claude_binary="/home/user/.local/bin/claude",
        self_repo_path="youruser/aegis",
        mcp_server_url="http://10.0.0.5:8080",
        api_key="SUPER-SECRET-KEY",
    )


def _claude_run_procs(list_output: bytes = b"@0:bash:0\n") -> list[AsyncMock]:
    """Subprocess mocks for a claude launch WITH the MCP mount:
    0 test-d, 1 pull, 2 worktree(+skills), 3 prompt, 4 mcp config,
    5 tmux ensure+list, 6 tmux new-window."""
    procs = _make_proc_sequence([0, 0, 0, 0, 0, 0, 0])
    procs[5].communicate = AsyncMock(return_value=(list_output, b""))
    return procs


def test_mcp_run_config_shape():
    """One server named `aegis`, pointed at THIS agent's endpoint, with the key
    in a header (never the query string — URLs land in access logs)."""
    cfg = json.loads(_mcp_run_config("pandoras-actor", "http://10.0.0.5:8080", "KEY123"))
    assert list(cfg["mcpServers"]) == ["aegis"]
    server = cfg["mcpServers"]["aegis"]
    assert server["type"] == "http"
    assert server["url"] == "http://10.0.0.5:8080/api/mcp-server/pandoras-actor"
    assert server["headers"] == {"X-API-Key": "KEY123"}


def test_mcp_run_config_normalizes_trailing_slash():
    """A base URL with a trailing slash must not produce `//api/mcp-server/...`."""
    cfg = json.loads(_mcp_run_config("sebas", "http://10.0.0.5:8080/", "K"))
    assert cfg["mcpServers"]["aegis"]["url"] == "http://10.0.0.5:8080/api/mcp-server/sebas"
    # ...and neither must several of them.
    cfg2 = json.loads(_mcp_run_config("sebas", "http://10.0.0.5:8080///", "K"))
    assert cfg2["mcpServers"]["aegis"]["url"] == "http://10.0.0.5:8080/api/mcp-server/sebas"


def test_agent_launch_flags_mcp_config_added_only_when_supplied():
    """`--mcp-config` + `--strict-mcp-config` appear exactly when a config path
    is passed, and the pair is inseparable: without `--strict-mcp-config` a
    `.mcp.json` in the target repo would add its own servers to an unattended
    full-auto run."""
    with_mcp = _agent_launch_flags(
        "claude", "/bin/claude", "/w", "/p", "", "$HOME/.aegis/mcp-sebas.json"
    )
    assert "--mcp-config $HOME/.aegis/mcp-sebas.json" in with_mcp
    assert "--strict-mcp-config" in with_mcp
    # The path must stay UNQUOTED so the remote shell expands $HOME.
    assert "'$HOME" not in with_mcp

    without = _agent_launch_flags("claude", "/bin/claude", "/w", "/p")
    assert "--mcp-config" not in without
    assert "--strict-mcp-config" not in without
    # Unchanged from the pre-mount form (falsifiable: appending the flags
    # unconditionally breaks this exact-equality assert).
    assert without == (
        "/bin/claude --print --output-format stream-json "
        "--verbose --dangerously-skip-permissions < /p"
    )


def test_agent_launch_flags_kimi_never_takes_mcp_config():
    """kimi CLI has no --mcp-config; passing one must not splice an unknown flag
    into its argv (which would kill the run outright)."""
    flags = _agent_launch_flags("kimi", "/bin/kimi", "/w", "/p", "", "$HOME/.aegis/mcp-x.json")
    assert flags == '/bin/kimi --output-format stream-json -p "$(cat /p)"'


def test_mcp_config_path_sanitizes_agent_id():
    """The path is used UNQUOTED remotely (so $HOME expands), so the variable
    part may only ever contain [A-Za-z0-9_-]."""
    assert _mcp_config_path("pandoras-actor") == "$HOME/.aegis/mcp-pandoras-actor.json"
    assert _mcp_config_path("a b;rm -rf /") == "$HOME/.aegis/mcp-a-b-rm--rf--.json"
    assert _mcp_config_path("") == "$HOME/.aegis/mcp-agent.json"


@pytest.mark.asyncio
async def test_claude_run_copies_skills_into_the_worktree(conn_mount):
    """self_repo_path set → the worktree command also seeds
    <worktree>/.claude/skills from the aegis checkout's config/skills."""
    with patch("asyncio.create_subprocess_exec", side_effect=_claude_run_procs()) as mock_exec:
        result = await conn_mount.start_kimi_run(
            repo="bcp", prompt="x", kimi_binary="/k", github_repo="Acme/bcp", agent_id="sebas"
        )
    assert result["status"] == "running"
    all_cmds = [" ".join(str(a) for a in c.args) for c in mock_exec.call_args_list]
    wt_cmd = next(c for c in all_cmds if "worktree add --detach" in c)
    # Source resolves through repo_base, destination is inside the run worktree.
    assert "/home/user/Workspace/youruser/aegis/config/skills" in wt_cmd
    assert ".claude/skills" in wt_cmd
    assert "cp -r" in wt_cmd
    # Guarded + rc-neutral: a missing skills dir must not fail the worktree step.
    assert "[ -d " in wt_cmd
    assert "|| true" in wt_cmd


@pytest.mark.asyncio
async def test_skills_copy_omitted_when_self_repo_path_unset(conn_claude):
    """conn_claude has no self_repo_path → no copy fragment anywhere. This is
    the falsifiability control for the test above."""
    procs = _make_proc_sequence([0, 0, 0, 0, 0, 0])
    procs[4].communicate = AsyncMock(return_value=(b"@0:bash:0\n", b""))
    with patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec:
        await conn_claude.start_kimi_run(
            repo="bcp", prompt="x", kimi_binary="/k", github_repo="Acme/bcp", agent_id="sebas"
        )
    all_cmds = [" ".join(str(a) for a in c.args) for c in mock_exec.call_args_list]
    assert all(".claude/skills" not in c for c in all_cmds)


@pytest.mark.asyncio
async def test_kimi_run_gets_neither_skills_nor_mcp(conn_mount):
    """v1 limitation, asserted so it can't drift silently: a kimi run copies no
    skills and writes no MCP config (5 ssh calls — no config-write step)."""
    procs = _make_proc_sequence([0, 0, 0, 0, 0])
    with patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec:
        result = await conn_mount.start_kimi_run(
            repo="aegis",
            prompt="x",
            kimi_binary="/k",
            github_repo="youruser/aegis",  # non-org → kimi
            agent_id="sebas",
        )
    assert result["engine"] == "kimi"
    all_cmds = [" ".join(str(a) for a in c.args) for c in mock_exec.call_args_list]
    assert all(".claude/skills" not in c for c in all_cmds)
    assert all("mcp-sebas.json" not in c for c in all_cmds)
    assert mock_exec.call_count == 5


@pytest.mark.asyncio
async def test_mcp_config_written_via_stdin_and_key_never_in_argv(conn_mount):
    """The API key reaches the host through the SSH channel's STDIN only.

    Falsifiable by construction: moving the config into the command line (an
    `echo '<json>' >` or a heredoc) puts the key into argv — visible in `ps` on
    a shared coding host and in shell audit logs — and fails this test.
    """
    procs = _claude_run_procs()
    with (
        patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec,
        structlog.testing.capture_logs() as logs,
    ):
        result = await conn_mount.start_kimi_run(
            repo="bcp", prompt="x", kimi_binary="/k", github_repo="Acme/bcp", agent_id="sebas"
        )
    assert result["status"] == "running"

    all_cmds = [" ".join(str(a) for a in c.args) for c in mock_exec.call_args_list]
    assert all("SUPER-SECRET-KEY" not in c for c in all_cmds), "API key leaked into argv"

    # The write itself: umask 077, outside the worktree, content over stdin.
    write_cmd = next(c for c in all_cmds if "mcp-sebas.json" in c and "cat >" in c)
    assert "umask 077" in write_cmd
    assert "-aegis-wt/" not in write_cmd  # never inside the run's own worktree
    payload = procs[4].communicate.await_args.kwargs["input"]
    cfg = json.loads(payload.decode())
    assert cfg["mcpServers"]["aegis"]["headers"]["X-API-Key"] == "SUPER-SECRET-KEY"
    assert cfg["mcpServers"]["aegis"]["url"] == "http://10.0.0.5:8080/api/mcp-server/sebas"

    # The launch mounts it, strictly.
    launch = all_cmds[-1]
    assert "--mcp-config $HOME/.aegis/mcp-sebas.json" in launch
    assert "--strict-mcp-config" in launch

    # Logged by path only — never the content.
    written = [log for log in logs if log["event"] == "mcp_config_written"]
    assert len(written) == 1
    assert written[0]["agent_id"] == "sebas"
    assert "SUPER-SECRET-KEY" not in json.dumps(logs)


@pytest.mark.asyncio
async def test_mcp_config_write_failure_launches_degraded_not_dead(conn_mount):
    """A failed config write costs the run its AEGIS tools, not its life."""
    procs = _claude_run_procs()
    procs[4].returncode = 1  # the `cat > mcp config` step fails remotely
    procs[4].communicate = AsyncMock(return_value=(b"", b"Permission denied"))
    with (
        patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec,
        structlog.testing.capture_logs() as logs,
    ):
        result = await conn_mount.start_kimi_run(
            repo="bcp", prompt="x", kimi_binary="/k", github_repo="Acme/bcp", agent_id="sebas"
        )
    assert result["status"] == "running"
    launch = " ".join(str(a) for a in mock_exec.call_args_list[-1].args)
    assert "--mcp-config" not in launch
    assert [log for log in logs if log["event"] == "mcp_config_write_failed"]


@pytest.mark.asyncio
async def test_no_mcp_mount_without_agent_id(conn_mount):
    """No agent_id ⇒ no endpoint to point at (the URL is per-agent), so no
    config write and no flags — 6 ssh calls, not 7."""
    procs = _make_proc_sequence([0, 0, 0, 0, 0, 0])
    procs[4].communicate = AsyncMock(return_value=(b"@0:bash:0\n", b""))
    with patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec:
        await conn_mount.start_kimi_run(
            repo="bcp", prompt="x", kimi_binary="/k", github_repo="Acme/bcp"
        )
    assert mock_exec.call_count == 6
    all_cmds = [" ".join(str(a) for a in c.args) for c in mock_exec.call_args_list]
    assert all("--mcp-config" not in c for c in all_cmds)


@pytest.mark.asyncio
async def test_mcp_mount_skipped_visibly_when_api_key_unset():
    """URL configured but no key: skipping is correct, doing it SILENTLY is not
    — an operator who set the URL expects the mount and would otherwise see a
    toolless run with no explanation."""
    conn = RemoteScriptConnector(
        host="node-a",
        user="user",
        key_file="/tmp/fake_key",
        repo_base="/home/user/Workspace",
        claude_orgs="acme",
        claude_binary="/bin/claude",
        mcp_server_url="http://10.0.0.5:8080",
        api_key="",
    )
    procs = _make_proc_sequence([0, 0, 0, 0, 0, 0])
    procs[4].communicate = AsyncMock(return_value=(b"@0:bash:0\n", b""))
    with (
        patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec,
        structlog.testing.capture_logs() as logs,
    ):
        await conn.start_kimi_run(
            repo="bcp", prompt="x", kimi_binary="/k", github_repo="Acme/bcp", agent_id="sebas"
        )
    skipped = [log for log in logs if log["event"] == "mcp_mount_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "api_key_unset"
    launch = " ".join(str(a) for a in mock_exec.call_args_list[-1].args)
    assert "--mcp-config" not in launch


# ── gated runs (human-in-the-loop permission gate) ─────────────────────────


def test_agent_launch_flags_ungated_claude_is_byte_identical():
    """Snapshot of the pre-gate command. `gated=False` is the overwhelming
    majority of runs, so the gate must be provably invisible to them — a stray
    flag or env prefix leaking into the default path fails right here."""
    assert _agent_launch_flags("claude", "/bin/claude", "/w", "/p", gated=False) == (
        "/bin/claude --print --output-format stream-json "
        "--verbose --dangerously-skip-permissions < /p"
    )
    assert _agent_launch_flags(
        "claude", "/bin/claude", "/w", "/p", "/cfg", "$HOME/.aegis/mcp-sebas.json", False
    ) == (
        "CLAUDE_CONFIG_DIR=/cfg /bin/claude --print --output-format stream-json "
        "--verbose --dangerously-skip-permissions "
        "--mcp-config $HOME/.aegis/mcp-sebas.json --strict-mcp-config < /p"
    )
    # Default is ungated: omitting the argument must not change anything.
    assert _agent_launch_flags("claude", "/bin/claude", "/w", "/p") == _agent_launch_flags(
        "claude", "/bin/claude", "/w", "/p", gated=False
    )


def test_agent_launch_flags_gated_swaps_skip_permissions_for_the_prompt_tool():
    """The two are mutually exclusive: leaving --dangerously-skip-permissions in
    would auto-allow everything, so the prompt tool would never fire and the
    'gate' would approve every action without asking anyone."""
    flags = _agent_launch_flags(
        "claude", "/bin/claude", "/w", "/p", "", "$HOME/.aegis/mcp-sebas.json", True
    )
    assert "--dangerously-skip-permissions" not in flags
    assert "--permission-prompt-tool mcp__aegis__approve_tool_use" in flags
    # The MCP mount survives — the prompt tool lives on that very server.
    assert "--mcp-config $HOME/.aegis/mcp-sebas.json --strict-mcp-config" in flags
    assert "--print --output-format stream-json --verbose" in flags


def test_agent_launch_flags_gated_exports_the_mcp_tool_timeout():
    """The approval call blocks on a human. Without this the CLI abandons it
    after its default per-tool timeout and the gate becomes a hang."""
    flags = _agent_launch_flags(
        "claude", "/bin/claude", "/w", "/p", "", "$HOME/.aegis/mcp-x.json", True
    )
    assert flags.startswith("MCP_TOOL_TIMEOUT=600000 /bin/claude")
    # 10 minutes in ms, and it MUST outlast core's 9-minute approval cap or a
    # slow operator surfaces as a transport failure instead of a deny.
    assert MCP_TOOL_TIMEOUT_MS == 600_000
    assert MCP_TOOL_TIMEOUT_MS / 1000 > AGENT_RUN_APPROVAL_TIMEOUT_S

    # With a config dir too: both env vars, CLAUDE_CONFIG_DIR still first.
    with_cfg = _agent_launch_flags(
        "claude", "/bin/claude", "/w", "/p", "/cfg", "$HOME/.aegis/mcp-x.json", True
    )
    assert with_cfg.startswith("CLAUDE_CONFIG_DIR=/cfg MCP_TOOL_TIMEOUT=600000 /bin/claude")
    # Ungated never exports it.
    assert "MCP_TOOL_TIMEOUT" not in _agent_launch_flags("claude", "/bin/claude", "/w", "/p")


def test_permission_prompt_tool_matches_the_tool_core_actually_serves():
    """`mcp__<server>__<tool>`: the server half must be the key written into the
    run's MCP config, the tool half the name core's MCP server advertises. A
    rename on either side makes every gated tool use unresolvable — and this
    assertion is the only place the two modules meet."""
    cfg = json.loads(_mcp_run_config("sebas", "http://10.0.0.5:8080", "K"))
    server = next(iter(cfg["mcpServers"]))
    assert f"mcp__{server}__{APPROVAL_TOOL_NAME}" == PERMISSION_PROMPT_TOOL
    assert PERMISSION_PROMPT_TOOL == "mcp__aegis__approve_tool_use"


@pytest.mark.asyncio
async def test_gated_kimi_run_fails_instead_of_running_ungated(conn_mount):
    """kimi has no --permission-prompt-tool. Silently downgrading to an ungated
    run is the one outcome that must never happen: the caller asked for a human
    in the loop and would get an unattended full-auto session instead."""
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        result = await conn_mount.start_kimi_run(
            repo="aegis",
            prompt="x",
            kimi_binary="/k",
            github_repo="youruser/aegis",  # non-org → routes to kimi
            agent_id="sebas",
            gated=True,
        )
    assert result["status"] == "failed"
    assert "claude" in result["error"]
    assert result["engine"] == "kimi"
    # Refused before touching the host at all — no worktree, no prompt file.
    assert mock_exec.call_count == 0


@pytest.mark.asyncio
async def test_gated_run_without_the_mcp_mount_fails(conn_claude):
    """--permission-prompt-tool names a tool on the `aegis` MCP server. Without
    the mount the CLI cannot reach it, so every gated action would fail or hang.
    conn_claude has no mcp_server_url, so the mount is skipped."""
    procs = _make_proc_sequence([0, 0, 0, 0])
    with patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec:
        result = await conn_claude.start_kimi_run(
            repo="bcp", prompt="x", kimi_binary="/k", github_repo="Acme/bcp",
            agent_id="sebas", gated=True,
        )
    assert result["status"] == "failed"
    assert "mcp" in result["error"].lower()
    assert result["engine"] == "claude"
    # It got as far as the mount attempt and stopped there — never launched.
    all_cmds = [" ".join(str(a) for a in c.args) for c in mock_exec.call_args_list]
    assert all("--permission-prompt-tool" not in c for c in all_cmds)
    assert all("/bin/claude --print" not in c for c in all_cmds)


@pytest.mark.asyncio
async def test_gated_claude_run_launches_with_the_gate_in_tmux(conn_mount):
    """The full gated launch: prompt tool in, skip-permissions out, timeout env
    exported, AEGIS tools mounted strictly."""
    with patch("asyncio.create_subprocess_exec", side_effect=_claude_run_procs()) as mock_exec:
        result = await conn_mount.start_kimi_run(
            repo="bcp", prompt="x", kimi_binary="/k", github_repo="Acme/bcp",
            agent_id="sebas", gated=True,
        )
    assert result["status"] == "running"
    assert result["engine"] == "claude"
    launch = " ".join(str(a) for a in mock_exec.call_args_list[-1].args)
    assert "--permission-prompt-tool mcp__aegis__approve_tool_use" in launch
    assert "--dangerously-skip-permissions" not in launch
    assert "MCP_TOOL_TIMEOUT=600000" in launch
    assert "--mcp-config $HOME/.aegis/mcp-sebas.json --strict-mcp-config" in launch


@pytest.mark.asyncio
async def test_ungated_claude_run_is_unchanged_by_the_gate(conn_mount):
    """Falsifiability control for the test above: the same launch without
    `gated` must still be the pre-gate command."""
    with patch("asyncio.create_subprocess_exec", side_effect=_claude_run_procs()) as mock_exec:
        result = await conn_mount.start_kimi_run(
            repo="bcp", prompt="x", kimi_binary="/k", github_repo="Acme/bcp", agent_id="sebas"
        )
    assert result["status"] == "running"
    launch = " ".join(str(a) for a in mock_exec.call_args_list[-1].args)
    assert "--dangerously-skip-permissions" in launch
    assert "--permission-prompt-tool" not in launch
    assert "MCP_TOOL_TIMEOUT" not in launch
