"""create_github_pr must push from the host where kimi made the branch."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aegis.connectors.remote_script import RemoteScriptConnector
from aegis_worker.activities.alert_governance import (
    AlertGovernanceActivities,
    CreateGithubPrInput,
)


class _FakePool:
    def __init__(self, row):
        self._row = row

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *a):
                return False

        return _Ctx()

    async def fetchrow(self, *a, **k):
        return self._row

    async def execute(self, *a, **k):
        return None


@pytest.mark.asyncio
async def test_create_github_pr_pushes_from_threaded_host():
    rs = RemoteScriptConnector(
        host="node-a", user="user", key_file="/tmp/k", repo_base="/home/user/Workspace"
    )
    pool = _FakePool({"title": "fix", "body": "b", "branch": "aegis-fix/x"})
    acts = AlertGovernanceActivities(db_pool=pool, remote_script=rs)

    proc = AsyncMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"https://github.com/o/r/pull/1\n", b""))
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec, patch(
        "aegis_worker.activities.alert_governance.activity"
    ):
        await acts.create_github_pr(
            CreateGithubPrInput(
                pending_pr_id="p1", repo="o/r", branch="aegis-fix/x", host="node-b"
            )
        )

    all_cmds = [" ".join(str(a) for a in c.args) for c in mock_exec.call_args_list]
    assert all_cmds, "expected at least the git push ssh call"
    assert all("user@node-b" in c for c in all_cmds)
    assert all("user@node-a" not in c for c in all_cmds)


@pytest.mark.asyncio
async def test_create_github_pr_uses_workspace_relative_repo_path():
    """The push/pr commands must cd into repo_base/<repo_path> (the resource's
    nested workspace path), not a recomputed flat repo_base/<basename>."""
    rs = RemoteScriptConnector(
        host="node-a", user="user", key_file="/tmp/k", repo_base="/home/user/Workspace"
    )
    pool = _FakePool({"title": "fix", "body": "b", "branch": "aegis-fix/x"})
    acts = AlertGovernanceActivities(db_pool=pool, remote_script=rs)

    proc = AsyncMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"https://github.com/acme/bcp/pull/9\n", b""))
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec, patch(
        "aegis_worker.activities.alert_governance.activity"
    ):
        await acts.create_github_pr(
            CreateGithubPrInput(
                pending_pr_id="p1",
                repo="acme/bcp",
                branch="aegis-fix/x",
                repo_path="acme/bcp",
            )
        )

    all_cmds = [" ".join(str(a) for a in c.args) for c in mock_exec.call_args_list]
    assert any("/home/user/Workspace/acme/bcp" in c for c in all_cmds)


@pytest.mark.asyncio
async def test_create_github_pr_falls_back_to_basename_without_repo_path():
    """Backward compat: an empty repo_path keeps the old flat-layout behavior."""
    rs = RemoteScriptConnector(
        host="node-a", user="user", key_file="/tmp/k", repo_base="/home/user/Workspace"
    )
    pool = _FakePool({"title": "fix", "body": "b", "branch": "aegis-fix/x"})
    acts = AlertGovernanceActivities(db_pool=pool, remote_script=rs)

    proc = AsyncMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"https://github.com/o/r/pull/1\n", b""))
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec, patch(
        "aegis_worker.activities.alert_governance.activity"
    ):
        await acts.create_github_pr(
            CreateGithubPrInput(pending_pr_id="p1", repo="o/r", branch="aegis-fix/x")
        )

    all_cmds = [" ".join(str(a) for a in c.args) for c in mock_exec.call_args_list]
    assert any("/home/user/Workspace/r" in c for c in all_cmds)
