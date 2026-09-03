"""Per-task Claude sessions on RemoteScriptConnector.

Three things the task lane needs from the connector, all asserted at the
composed-shell level (SSH is stubbed at `_exec`):

  * `--session-id`/`-n` on a task's FIRST turn and `--resume` on every later
    one — never both, because a run that silently lost its `--resume` would
    start a fresh, amnesiac session under a new id;
  * a CALLER-OWNED worktree, which the launch path must neither provision nor
    remove — it outlives the run;
  * `ensure_task_worktree` / `kill_run`, the two operations the task flow needs
    that no per-run helper covers.
"""

from __future__ import annotations

import shlex

from aegis.connectors.remote_script import RemoteScriptConnector, _agent_launch_flags

SID = "5925e3ce-d1d9-539c-826c-011f67dcfa81"


def test_first_turn_gets_session_id_and_name_never_resume():
    cmd = _agent_launch_flags(
        "claude", "/bin/claude", "/w", "/p", session_id=SID, name="task 1: fix"
    )
    assert f"--session-id {SID}" in cmd and f"-n {shlex.quote('task 1: fix')}" in cmd
    assert "--resume" not in cmd


def test_later_turn_resumes_and_never_renames():
    cmd = _agent_launch_flags(
        "claude", "/bin/claude", "/w", "/p", session_id=SID, resume=True, name="x"
    )
    assert f"--resume {SID}" in cmd and "--session-id" not in cmd and " -n " not in cmd


def test_kimi_ignores_session_flags():
    cmd = _agent_launch_flags("kimi", "/bin/kimi", "/w", "/p", session_id=SID, name="x")
    assert "--session-id" not in cmd and "--resume" not in cmd


class _Exec:
    """Records every remote command; answers each with a canned result."""

    def __init__(self, answers=None):
        self.cmds: list[str] = []
        self.answers = answers or {}

    async def __call__(self, host, cmd, timeout, stdin=None, **kw):
        self.cmds.append(cmd)
        for needle, res in self.answers.items():
            if needle in cmd:
                return res
        return {"status": "succeeded", "exit_code": 0, "stdout": "", "stderr": ""}


def _connector():
    c = RemoteScriptConnector(
        host="h", user="u", key_file="/k", repo_base="/repos", claude_binary="/bin/claude"
    )
    c._config_expiry = float("inf")  # no DB refresh
    return c


async def test_ensure_task_worktree_is_idempotent_and_creates_on_branch(monkeypatch):
    c = _connector()
    ex = _Exec()
    monkeypatch.setattr(c, "_exec", ex)
    out = await c.ensure_task_worktree(
        "acme/app", "/repos/acme/app-aegis-wt/task-1", "aegis-task/1"
    )
    assert out["status"] == "ready"
    joined = "\n".join(ex.cmds)
    assert (
        "worktree add -b aegis-task/1" in joined
        and "worktree add /repos/acme/app-aegis-wt/task-1 aegis-task/1" in joined
    )
    assert "[ -d /repos/acme/app-aegis-wt/task-1 ]" in joined


async def test_start_run_with_caller_worktree_skips_provisioning_and_never_removes_it(monkeypatch):
    c = _connector()
    ex = _Exec({"cat > ": {"status": "failed", "exit_code": 1, "stdout": "", "stderr": "disk full"}})
    monkeypatch.setattr(c, "_exec", ex)
    out = await c.start_kimi_run(
        repo="acme/app",
        prompt="hi",
        kimi_binary="",
        engine_override="claude",
        worktree_path="/repos/acme/app-aegis-wt/task-1",
        session_id=SID,
    )
    assert out["status"] == "failed"
    joined = "\n".join(ex.cmds)
    assert (
        "worktree add" not in joined and "worktree remove" not in joined and "git pull" not in joined
    )


async def test_kill_run_uses_fuser(monkeypatch):
    c = _connector()
    ex = _Exec()
    monkeypatch.setattr(c, "_exec", ex)
    assert await c.kill_run("/tmp/aegis-kimi-run-1.jsonl") is True
    assert "fuser -k /tmp/aegis-kimi-run-1.jsonl" in ex.cmds[-1]
