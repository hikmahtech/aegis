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

from aegis.connectors.remote_script import (
    _PROMPT_CAP_BYTES,
    RemoteScriptConnector,
    _agent_launch_flags,
)

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
    """Records every remote command and its stdin; answers with a canned result."""

    def __init__(self, answers=None):
        self.cmds: list[str] = []
        self.stdins: list[bytes] = []
        self.answers = answers or {}

    async def __call__(self, host, cmd, timeout, stdin=None, **kw):
        self.cmds.append(cmd)
        self.stdins.append(stdin or b"")
        for needle, res in self.answers.items():
            if needle in cmd:
                return res
        return {"status": "succeeded", "exit_code": 0, "stdout": "", "stderr": ""}


def _written(ex) -> list[bytes]:
    return [s for c, s in zip(ex.cmds, ex.stdins, strict=True) if c.startswith("cat > ")]


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


async def test_a_long_turn_prompt_reaches_the_host_intact(monkeypatch):
    """A task turn's prompt carries the whole comment thread. The old 5000-byte
    ceiling cut it mid-sentence and said nothing — no error, no log — so the run
    was steered by half a conversation."""
    c = _connector()
    ex = _Exec()
    monkeypatch.setattr(c, "_exec", ex)
    prompt = "x" * 10_000
    await c.start_kimi_run(
        repo="acme/app",
        prompt=prompt,
        kimi_binary="",
        engine_override="claude",
        worktree_path="/repos/acme/app-aegis-wt/task-1",
        session_id=SID,
    )
    written = _written(ex)
    assert written and written[0] == prompt.encode()


async def _write_prompt(monkeypatch, prompt: str) -> bytes:
    c = _connector()
    ex = _Exec()
    monkeypatch.setattr(c, "_exec", ex)
    await c.start_kimi_run(
        repo="acme/app",
        prompt=prompt,
        kimi_binary="",
        engine_override="claude",
        worktree_path="/repos/acme/app-aegis-wt/task-1",
        session_id=SID,
    )
    return _written(ex)[0]


async def test_a_prompt_exactly_at_the_cap_is_written_whole(monkeypatch):
    """The flow sizes its prompt against this number, so the boundary is a
    contract between the two: at the cap nothing may be dropped."""
    prompt = "x" * _PROMPT_CAP_BYTES
    assert await _write_prompt(monkeypatch, prompt) == prompt.encode()


async def test_one_byte_over_the_cap_is_cut_to_a_valid_utf8_prefix(monkeypatch):
    """Cut on bytes, then drop the partial character the cut created. A prompt
    file with an invalid UTF-8 tail is unreadable as a whole, so the run would
    lose every byte rather than the last one."""
    # 23 999 ASCII bytes plus a 2-byte character = 24 001; the cut lands INSIDE
    # that character.
    prompt = "x" * (_PROMPT_CAP_BYTES - 1) + "é"
    assert len(prompt.encode()) == _PROMPT_CAP_BYTES + 1

    sent = await _write_prompt(monkeypatch, prompt)
    assert sent == b"x" * (_PROMPT_CAP_BYTES - 1)
    sent.decode("utf-8")  # raises if the half character survived


async def test_kill_run_uses_fuser(monkeypatch):
    c = _connector()
    ex = _Exec()
    monkeypatch.setattr(c, "_exec", ex)
    assert await c.kill_run("/tmp/aegis-kimi-run-1.jsonl") is True
    assert "fuser -k /tmp/aegis-kimi-run-1.jsonl" in ex.cmds[-1]
