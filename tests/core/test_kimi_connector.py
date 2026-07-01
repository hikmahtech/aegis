"""Unit tests for the new Kimi CLI methods on RemoteScriptConnector.

We mock asyncio.create_subprocess_exec to capture the SSH command shape.
Integration against real node-a is out of scope for unit tests.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aegis.connectors.remote_script import (
    RemoteScriptConnector,
    _agent_launch_flags,
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
    assert "--work-dir" in combined
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

    # The kimi launch (last command) must cd into + --work-dir the worktree, NOT bare repo_path
    kimi_cmd = all_cmds[-1]
    repo_path = "/home/user/Workspace/youruser/bcp"
    assert "--work-dir" in kimi_cmd
    # The --work-dir argument must reference the worktree path (contains -aegis-wt/)
    assert "-aegis-wt/" in kimi_cmd
    # And it must NOT cd into / use the bare repo_path for --work-dir (worktree is used instead)
    # (repo_path still appears in the worktree-add command, which is fine)
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

    # kimi must run with --work-dir pointing at the shared repo_path (not -aegis-wt/)
    assert "--work-dir" in kimi_cmd
    assert repo_path in kimi_cmd
    assert "-aegis-wt/" not in kimi_cmd


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
def conn_asif():
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
async def test_probe_host_uses_fast_batch_ssh_and_returns_true_on_rc0(conn_asif):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.returncode = 0
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec:
        ok = await conn_asif._probe_host("node-b")
    assert ok is True
    argv = " ".join(str(a) for a in mock_exec.call_args.args)
    assert "ConnectTimeout=3" in argv
    assert "BatchMode=yes" in argv
    assert "user@node-b" in argv


@pytest.mark.asyncio
async def test_probe_host_returns_false_on_nonzero(conn_asif):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.returncode = 255
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=255)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        ok = await conn_asif._probe_host("node-b")
    assert ok is False


@pytest.mark.asyncio
async def test_resolve_kimi_host_prefers_reachable_kimi_host(conn_asif):
    with patch.object(conn_asif, "_probe_host", AsyncMock(return_value=True)):
        host, use_tmux = await conn_asif._resolve_kimi_host()
    assert host == "node-b"
    assert use_tmux is True


@pytest.mark.asyncio
async def test_resolve_kimi_host_falls_back_when_probe_fails(conn_asif):
    with patch.object(conn_asif, "_probe_host", AsyncMock(return_value=False)):
        host, use_tmux = await conn_asif._resolve_kimi_host()
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
async def test_start_kimi_run_tmux_mode_launches_window_with_tee(conn_asif):
    """kimi_host reachable + under cap → tmux new-window with tee + remain-on-exit on node-b."""
    # _probe_host is patched (no subprocess), so the ssh calls are:
    #   0 test-d, 1 pull, 2 worktree, 3 prompt, 4 ensure+list, 5 new-window launch
    procs = _make_proc_sequence([0, 0, 0, 0, 0, 0])
    procs[4].communicate = AsyncMock(return_value=(b"@0:bash:0\n", b""))  # list-windows output
    with (
        patch.object(conn_asif, "_probe_host", AsyncMock(return_value=True)),
        patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec,
    ):
        result = await conn_asif.start_kimi_run(
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
async def test_start_kimi_run_tmux_all_live_falls_back_to_detached(conn_asif):
    """kimi_host reachable but all 10 windows live → detached nohup on node-b, in_tmux False."""
    live = "\n".join(f"@{i}:kimi-bcp-{i:04d}:0" for i in range(10))
    # _probe_host patched; ssh calls: 0 test-d, 1 pull, 2 worktree, 3 prompt,
    #   4 ensure+list (all live → skip tmux), 5 detached nohup fallback
    procs = _make_proc_sequence([0, 0, 0, 0, 0, 0])
    procs[4].communicate = AsyncMock(return_value=(live.encode(), b""))
    with (
        patch.object(conn_asif, "_probe_host", AsyncMock(return_value=True)),
        patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec,
    ):
        result = await conn_asif.start_kimi_run(
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
async def test_start_kimi_run_probe_fail_uses_meem_nohup(conn_asif):
    """kimi_host set but unreachable → node-a nohup, host=node-a, no probe-driven tmux."""
    procs = _make_proc_sequence([0, 0, 0, 0, 0])
    with (
        patch.object(conn_asif, "_probe_host", AsyncMock(return_value=False)),
        patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec,
    ):
        result = await conn_asif.start_kimi_run(
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


def test_engine_for_unset_orgs_always_kimi(conn_asif):
    assert conn_asif._engine_for("Acme/bcp") == "kimi"


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
    assert "--work-dir" in launch
    assert "--dangerously-skip-permissions" not in launch


@pytest.mark.asyncio
async def test_start_kimi_run_org_repo_without_claude_orgs_stays_kimi(conn_asif):
    """Dormant default: no claude_orgs configured → org repos keep today's kimi path."""
    procs = _make_proc_sequence([0, 0, 0, 0, 0, 0])
    procs[4].communicate = AsyncMock(return_value=(b"@0:bash:0\n", b""))
    with (
        patch.object(conn_asif, "_probe_host", AsyncMock(return_value=True)),
        patch("asyncio.create_subprocess_exec", side_effect=procs),
    ):
        result = await conn_asif.start_kimi_run(
            repo="bcp",
            prompt="x",
            kimi_binary="/usr/local/bin/kimi",
            github_repo="Acme/bcp",
        )
    assert result["engine"] == "kimi"
    assert result["host"] == "node-b"


@pytest.mark.asyncio
async def test_start_kimi_run_claude_binary_missing_fails_fast(conn_asif):
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
