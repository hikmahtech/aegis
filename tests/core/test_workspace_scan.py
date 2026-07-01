"""RemoteScriptConnector workspace methods — scan parsing, ensure_repo, host pick."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aegis.connectors.remote_script import (
    RemoteScriptConnector,
    _parse_workspace_scan,
)


def _conn(**kw):
    defaults = {
        "host": "node-a",
        "user": "user",
        "key_file": "/tmp/fake_key",
        "repo_base": "/home/user/Workspace",
    }
    defaults.update(kw)
    return RemoteScriptConnector(**defaults)


# ---------------------------------------------------------------------------
# _parse_workspace_scan
# ---------------------------------------------------------------------------


def test_parse_scan_basic_lines():
    out = _parse_workspace_scan(
        "acme/bcp|git@github.com:acme/bcp.git\n"
        "personal/aegis|git@github.com:youruser/aegis.git\n"
        "local-only|\n"
    )
    assert out == [
        {"path": "local-only", "origin_url": ""},
        {"path": "personal/aegis", "origin_url": "git@github.com:youruser/aegis.git"},
        {"path": "acme/bcp", "origin_url": "git@github.com:acme/bcp.git"},
    ]


def test_parse_scan_skips_aegis_worktrees_and_nested_repos():
    out = _parse_workspace_scan(
        "acme/bcp|git@github.com:acme/bcp.git\n"
        "acme/bcp/vendor|git@github.com:other/vendor.git\n"
        "bcp-aegis-wt/abc123|git@github.com:acme/bcp.git\n"
        "acme/bcp-aegis-wt/def456|git@github.com:acme/bcp.git\n"
        ".claude/worktrees/x|\n"
        "\n"
        "garbage-line-without-pipe\n"
    )
    assert out == [
        {"path": "acme/bcp", "origin_url": "git@github.com:acme/bcp.git"}
    ]


# ---------------------------------------------------------------------------
# workspace_scan_host
# ---------------------------------------------------------------------------


def test_workspace_scan_host_prefers_kimi_host():
    assert _conn(kimi_host="node-b").workspace_scan_host() == "node-b"
    assert _conn().workspace_scan_host() == "node-a"


# ---------------------------------------------------------------------------
# list_workspace_repos
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_workspace_repos_targets_canonical_host_and_parses():
    conn = _conn(kimi_host="node-b")
    proc = AsyncMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(
        return_value=(b"personal/aegis|git@github.com:youruser/aegis.git\n", b"")
    )
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec:
        repos = await conn.list_workspace_repos()
    assert repos == [
        {"path": "personal/aegis", "origin_url": "git@github.com:youruser/aegis.git"}
    ]
    cmd = " ".join(str(a) for a in mock_exec.call_args.args)
    assert "user@node-b" in cmd
    assert "find . -mindepth 2 -maxdepth 3 -name .git" in cmd


@pytest.mark.asyncio
async def test_list_workspace_repos_raises_on_scan_failure():
    conn = _conn(kimi_host="node-b")
    proc = AsyncMock()
    proc.returncode = 255
    proc.communicate = AsyncMock(return_value=(b"", b"ssh: connect refused"))
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=255)
    with (
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        pytest.raises(RuntimeError, match="workspace scan failed"),
    ):
        await conn.list_workspace_repos()


@pytest.mark.asyncio
async def test_list_workspace_repos_requires_repo_base():
    conn = _conn(repo_base="")
    with pytest.raises(RuntimeError, match="repo_base"):
        await conn.list_workspace_repos()


# ---------------------------------------------------------------------------
# ensure_repo
# ---------------------------------------------------------------------------


def _proc(rc: int, stdout: bytes = b"", stderr: bytes = b""):
    proc = AsyncMock()
    proc.returncode = rc
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=rc)
    return proc


@pytest.mark.asyncio
async def test_ensure_repo_present_skips_clone():
    conn = _conn()
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_proc(0))) as mock_exec:
        result = await conn.ensure_repo("acme/bcp", "git@github.com:acme/bcp.git")
    assert result["status"] == "present"
    assert mock_exec.call_count == 1  # only the test -d


@pytest.mark.asyncio
async def test_ensure_repo_clones_into_nested_path():
    conn = _conn()
    procs = [_proc(1), _proc(0)]  # test -d fails → clone succeeds
    with patch(
        "asyncio.create_subprocess_exec", AsyncMock(side_effect=procs)
    ) as mock_exec:
        result = await conn.ensure_repo("acme/bcp", "git@github.com:acme/bcp.git")
    assert result["status"] == "cloned"
    clone_cmd = " ".join(str(a) for a in mock_exec.call_args_list[1].args)
    assert "mkdir -p /home/user/Workspace/acme" in clone_cmd
    assert (
        "git clone git@github.com:acme/bcp.git "
        "/home/user/Workspace/acme/bcp" in clone_cmd
    )


@pytest.mark.asyncio
async def test_ensure_repo_rejects_traversal_and_missing_origin():
    conn = _conn()
    result = await conn.ensure_repo("../etc", "git@github.com:x/y.git")
    assert result["status"] == "failed"

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_proc(1))):
        result = await conn.ensure_repo("no-origin", "")
    assert result["status"] == "failed"
    assert "origin" in result["error"]
