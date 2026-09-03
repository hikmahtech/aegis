# Task Sessions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the coding lane into one persistent Claude Code session per `@code` task, driven turn by turn by Todoist comments and Slack thread replies, with task-level ownership and a same-task handoff to the operator.

**Architecture:** A `task_sessions` row per task carries the session uuid, worktree and branch. `AgentTaskFlow` (workflow id `agent-task-<task_id>`) runs one `claude -p` turn per comment, resumes the session with `--resume`, posts the final message as a task comment, and parks. Comments reach the flow by signal-with-start from the Todoist webhook (fast path) and the 15-minute sweep (fallback). Before each turn a collision check consults `claude agents --json` and, when the operator has sessions in the same repo, one LLM call decides whether they are already on this task.

**Tech Stack:** Python 3.12, Temporal (temporalio), asyncpg, FastAPI, httpx, Claude Code CLI 2.1.259 (`--session-id`, `--resume`, `-n`), pytest with `-n auto --dist loadfile`.

**Spec:** `docs/superpowers/specs/2026-09-03-task-sessions-design.md`

## Global Constraints

- Test commands run ONE package at a time: `PYTHONPATH=core/src:worker/src:comms/src .venv/bin/python -m pytest tests/<pkg>/<file> -n auto --dist loadfile --timeout=300`. Never `pytest` bare. Postgres must be up (`docker compose up -d postgres`, port 25432). The venv is at the repo root: `/home/arshad/Workspace/hikmah/aegis/.venv`.
- Lint: `ruff check core/src/ tests/core/` (and `worker/src/ tests/worker/`, `comms/src/ tests/comms/`). NEVER run `ruff format` on `core/src/aegis/services/chat.py` or `core/src/aegis/services/tools/infra.py`.
- Every new test must be proven falsifiable once: break the code, see the test fail, revert. Fakes must match the real class's method names and signatures.
- Every `@activity.defn` method on an `AgentTaskActivities` instance is auto-collected; a new activity needs no registration. Flow dataclasses keep `agent_id` first.
- `AgentTaskFlowInput.todoist_task_id` keeps that exact name (the run recorder reads it).
- Commit after every task, single-line semantic messages, no co-author trailer. Work on branch `worktree-task-sessions` in this worktree.
- Comments AEGIS posts go through `AgentTaskActivities.comment`, which appends the `Workflow run:` footer. A note without that footer (and without the `[ClarifyFlow @ ` / `[Agent reply @ ` prefixes) is a USER note and triggers a turn. Never post AEGIS output without the footer.
- The plan below is PR 1 (tasks 1–7) then PR 2 (tasks 8–12). PR 1 must be green and mergeable on its own.

---

## PR 1 — sessions and turns

### Task 1: `task_sessions` table and service

**Files:**
- Create: `migrations/025_task_sessions.sql`
- Create: `core/src/aegis/services/task_sessions.py`
- Test: `tests/core/test_task_sessions_service.py`

**Interfaces:**
- Produces (all async, `pool` is an asyncpg pool):
  - `get_session(pool, task_id: str) -> dict | None`
  - `create_session(pool, *, task_id: str, agent_id: str) -> dict` — inserts with a fresh `uuid4` session id, `ON CONFLICT (task_id) DO NOTHING`, then returns the row (existing or new). `session_id` is returned as a `str`.
  - `set_repo(pool, task_id, *, repo: str, github_repo: str, worktree_path: str, branch: str, host: str) -> None`
  - `record_turn(pool, task_id, *, launched: bool) -> None` — `last_turn_at = now()`, and `turns = turns + 1` only when `launched`.
  - `set_slack_ref(pool, task_id, ref: dict) -> None`
  - `find_by_thread(pool, channel: str, ts: str) -> str | None` — task id whose `slack_ref` matches.
  - `find_turns_due(pool, limit: int = 20) -> list[dict]` — `[{"task_id", "agent_id", "comment"}]`.
  - `is_user_note(content: str) -> bool` — False when content starts with `CLARIFY_NOTE_PREFIX` or `AGENT_REPLY_PREFIX` or contains `Workflow run:`.
  - `dispatch_task_turn(client, *, task_id, agent_id, comment, task_queue="aegis-main") -> str` — returns `"started"` or `"signalled"` (client is a `temporalio.client.Client`).

- [ ] **Step 1: Write the migration**

```sql
-- task_sessions: one persistent Claude Code session per @code Todoist task.
-- The comment thread on the task is the control channel; this row is what
-- lets a later comment resume the same session in the same worktree.
CREATE TABLE IF NOT EXISTS task_sessions (
    task_id       text PRIMARY KEY,
    agent_id      text NOT NULL,
    session_id    uuid NOT NULL,
    repo          text NOT NULL DEFAULT '',   -- workspace-relative checkout path; '' = not yet resolved
    github_repo   text NOT NULL DEFAULT '',
    worktree_path text NOT NULL DEFAULT '',
    branch        text NOT NULL DEFAULT '',
    host          text NOT NULL DEFAULT '',
    slack_ref     jsonb,                      -- {"channel","ts"} thread root; NULL until first delivery
    turns         int  NOT NULL DEFAULT 0,
    last_turn_at  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);
```

- [ ] **Step 2: Write the failing tests** (real DB; use the `db_pool` fixture like `tests/worker/activities/test_agent_task_eligibility.py`)

```python
"""aegis.services.task_sessions — row lifecycle, due-turn query, dispatch dance."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aegis.clarify_note import AGENT_REPLY_PREFIX, CLARIFY_NOTE_PREFIX
from aegis.services import task_sessions as svc
from temporalio.exceptions import WorkflowAlreadyStartedError

_TASK = "ts-task-1"


@pytest_asyncio.fixture(loop_scope="function")
async def _clean(db_pool):
    for sql in (
        "DELETE FROM task_sessions WHERE task_id = $1",
        "DELETE FROM todoist_notes WHERE item_id = $1",
        "DELETE FROM todoist_tasks WHERE id = $1",
    ):
        await db_pool.execute(sql, _TASK)
    await db_pool.execute(
        "INSERT INTO todoist_tasks (id, content, labels, is_completed, updated_at) "
        "VALUES ($1, 'fix it', ARRAY['@pandora','@code'], false, now())",
        _TASK,
    )
    yield
    for sql in (
        "DELETE FROM task_sessions WHERE task_id = $1",
        "DELETE FROM todoist_notes WHERE item_id = $1",
        "DELETE FROM todoist_tasks WHERE id = $1",
    ):
        await db_pool.execute(sql, _TASK)


async def _note(db_pool, content: str, age: str = "0 seconds"):
    await db_pool.execute(
        "INSERT INTO todoist_notes (id, item_id, content, posted_at, raw) "
        "VALUES ($1, $2, $3, now() - $4::interval, '{}')",
        str(uuid.uuid4()), _TASK, content, age,
    )


async def test_create_is_idempotent_and_mints_one_session_id(db_pool, _clean):
    a = await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    b = await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    assert a["session_id"] == b["session_id"]
    uuid.UUID(a["session_id"])  # a valid uuid string
    assert a["turns"] == 0 and a["repo"] == ""


async def test_record_turn_counts_only_launched_turns(db_pool, _clean):
    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    await svc.record_turn(db_pool, _TASK, launched=False)
    await svc.record_turn(db_pool, _TASK, launched=True)
    row = await svc.get_session(db_pool, _TASK)
    assert row["turns"] == 1 and row["last_turn_at"] is not None


async def test_find_turns_due_sees_only_user_notes_newer_than_the_watermark(db_pool, _clean):
    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    await _note(db_pool, "please also fix the tests")
    due = await svc.find_turns_due(db_pool)
    assert [(d["task_id"], d["comment"]) for d in due] == [(_TASK, "please also fix the tests")]
    await svc.record_turn(db_pool, _TASK, launched=True)
    assert await svc.find_turns_due(db_pool) == []
    await _note(db_pool, f"[pandoras-actor] done\n\nWorkflow run: agent-task-{_TASK}")
    await _note(db_pool, CLARIFY_NOTE_PREFIX + "2026] filed")
    await _note(db_pool, AGENT_REPLY_PREFIX + "2026 agent=sebas] hi")
    assert await svc.find_turns_due(db_pool) == []


async def test_find_turns_due_skips_completed_tasks(db_pool, _clean):
    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    await _note(db_pool, "go")
    await db_pool.execute("UPDATE todoist_tasks SET is_completed = true WHERE id = $1", _TASK)
    assert await svc.find_turns_due(db_pool) == []


async def test_slack_ref_round_trip(db_pool, _clean):
    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    await svc.set_slack_ref(db_pool, _TASK, {"channel": "C1", "ts": "1.2"})
    assert await svc.find_by_thread(db_pool, "C1", "1.2") == _TASK
    assert await svc.find_by_thread(db_pool, "C1", "9.9") is None


def test_is_user_note():
    assert svc.is_user_note("please do X")
    assert not svc.is_user_note(CLARIFY_NOTE_PREFIX + "x")
    assert not svc.is_user_note(AGENT_REPLY_PREFIX + "x")
    assert not svc.is_user_note("[pandoras-actor] plan\n\nWorkflow run: agent-task-1")


@pytest.mark.asyncio
async def test_dispatch_starts_then_signals_then_restarts():
    client = MagicMock()
    client.start_workflow = AsyncMock()
    assert await svc.dispatch_task_turn(client, task_id="t1", agent_id="a", comment="go") == "started"
    kw = client.start_workflow.call_args.kwargs
    assert kw["id"] == "agent-task-t1" and kw["task_queue"] == "aegis-main"
    assert client.start_workflow.call_args.args[1]["comment"] == "go"

    handle = MagicMock(); handle.signal = AsyncMock()
    client.get_workflow_handle = MagicMock(return_value=handle)
    client.start_workflow = AsyncMock(side_effect=WorkflowAlreadyStartedError("agent-task-t1", "AgentTaskFlow"))
    assert await svc.dispatch_task_turn(client, task_id="t1", agent_id="a", comment="more") == "signalled"
    handle.signal.assert_awaited_once_with("comment", "more")

    handle.signal = AsyncMock(side_effect=RuntimeError("workflow execution already completed"))
    client.start_workflow = AsyncMock(side_effect=[WorkflowAlreadyStartedError("x", "y"), None])
    assert await svc.dispatch_task_turn(client, task_id="t1", agent_id="a", comment="again") == "started"
```

- [ ] **Step 3: Run tests to verify they fail** — `ModuleNotFoundError` / relation missing.

- [ ] **Step 4: Implement the service**

```python
"""task_sessions — one persistent coding session per @code Todoist task.

See docs/superpowers/specs/2026-09-03-task-sessions-design.md. The comment
thread on the task is the control channel; this module owns the row that lets
a later comment resume the same session, and the start-or-signal dispatch that
lands every comment on the task's single workflow.
"""
from __future__ import annotations

import uuid
from typing import Any

from temporalio.exceptions import WorkflowAlreadyStartedError

from aegis.clarify_note import AGENT_REPLY_PREFIX, CLARIFY_NOTE_PREFIX

_COLS = ("task_id, agent_id, session_id::text AS session_id, repo, github_repo, "
         "worktree_path, branch, host, slack_ref, turns, last_turn_at, created_at")


def is_user_note(content: str) -> bool:
    c = content or ""
    return not (c.startswith(CLARIFY_NOTE_PREFIX) or c.startswith(AGENT_REPLY_PREFIX) or "Workflow run:" in c)


async def get_session(pool, task_id: str) -> dict | None:
    row = await pool.fetchrow(f"SELECT {_COLS} FROM task_sessions WHERE task_id = $1", task_id)
    return dict(row) if row else None


async def create_session(pool, *, task_id: str, agent_id: str) -> dict:
    await pool.execute(
        "INSERT INTO task_sessions (task_id, agent_id, session_id) VALUES ($1, $2, $3) "
        "ON CONFLICT (task_id) DO NOTHING",
        task_id, agent_id, uuid.uuid4(),
    )
    row = await get_session(pool, task_id)
    assert row is not None
    return row
# set_repo / record_turn / set_slack_ref / find_by_thread: plain UPDATE/SELECT statements.
# slack_ref is jsonb: pass json.dumps(ref) and cast with ::jsonb; find_by_thread compares
# slack_ref->>'channel' and slack_ref->>'ts'.
```

`find_turns_due` SQL (bind `CLARIFY_NOTE_PREFIX + "%"`, `AGENT_REPLY_PREFIX + "%"`, `limit`):

```sql
SELECT ts.task_id, ts.agent_id, n.content AS comment
FROM task_sessions ts
JOIN todoist_tasks t ON t.id = ts.task_id AND NOT t.is_completed
JOIN LATERAL (
    SELECT content, posted_at FROM todoist_notes
    WHERE item_id = ts.task_id
      AND content NOT LIKE $1 AND content NOT LIKE $2
      AND content NOT LIKE '%Workflow run:%'
    ORDER BY posted_at DESC LIMIT 1
) n ON n.posted_at > COALESCE(ts.last_turn_at, ts.created_at)
ORDER BY n.posted_at ASC
LIMIT $3
```

`dispatch_task_turn`:

```python
async def dispatch_task_turn(client, *, task_id: str, agent_id: str, comment: str,
                             task_queue: str = "aegis-main") -> str:
    wf_id = f"agent-task-{task_id}"
    payload = {"agent_id": agent_id, "todoist_task_id": task_id, "task": {}, "comment": comment}

    async def _start() -> None:
        await client.start_workflow("AgentTaskFlow", payload, id=wf_id, task_queue=task_queue)

    try:
        await _start()
        return "started"
    except WorkflowAlreadyStartedError:
        pass
    try:
        await client.get_workflow_handle(wf_id).signal("comment", comment)
        return "signalled"
    except Exception:  # noqa: BLE001 — the flow completed between the two calls
        await _start()
        return "started"
```

- [ ] **Step 5: Run the tests, then ruff; commit** — `feat(core): task_sessions table, service and turn dispatch`

### Task 2: connector — session flags, caller-owned worktree, `ensure_task_worktree`, `kill_run`

**Files:**
- Modify: `core/src/aegis/connectors/remote_script.py` (`_agent_launch_flags` ~155, `_launch_in_tmux` ~1274, `start_kimi_run` ~853, new methods next to `remove_worktree` ~1492)
- Test: `tests/core/test_remote_script_task_sessions.py`

**Interfaces:**
- `_agent_launch_flags(engine, binary, work_path, prompt_file, config_dir="", mcp_config="", gated=False, session_id="", resume=False, name="") -> str`. For `engine == "claude"` and a non-empty `session_id`: insert ` --resume <id>` when `resume`, else ` --session-id <id>` plus ` -n <name>` when `name` is non-empty; the fragment goes right before the permission flag. Kimi ignores all three.
- `start_kimi_run(..., session_id="", resume=False, name="", worktree_path="")`. A non-empty `worktree_path` means the caller owns the worktree: skip the `test -d` / `git pull` / `worktree add` phases, use it as `work_path`, return it as `worktree_path`, and NEVER `remove_worktree` it on a failure path (introduce a local `owns_worktree = not worktree_path` and guard the three existing `remove_worktree` calls with it). Thread `session_id`, `resume`, `name` into `_launch_in_tmux` and the nohup command.
- `ensure_task_worktree(self, repo: str, worktree_path: str, branch: str, host: str = "") -> dict` — `{"status": "ready"|"failed", "error": str}`. Shell: pull the shared checkout (`git -C <repo_path> pull --ff-only --quiet 2>/dev/null || true`), then `[ -d <wt> ] && exit 0`, else `mkdir -p <parent> && (git -C <repo_path> worktree add -b <branch> <wt> 2>/dev/null || git -C <repo_path> worktree add <wt> <branch>)` followed by `_skills_copy_fragment(self._skills_source_dir(), worktree_path)`. A missing `repo_path` (`test -d` fails) is `failed` with the same "Repo checkout missing" wording `start_kimi_run` uses.
- `kill_run(self, output_file: str, host: str = "") -> bool` — `fuser -k <output_file> >/dev/null 2>&1; true`; returns True when the ssh call succeeded.

- [ ] **Step 1: Write the failing tests** (pure flag tests plus a fake-`_exec` connector; follow `tests/core/test_coding_session_inventory.py::_connector` for construction)

```python
import shlex
from aegis.connectors.remote_script import RemoteScriptConnector, _agent_launch_flags

SID = "5925e3ce-d1d9-539c-826c-011f67dcfa81"


def test_first_turn_gets_session_id_and_name_never_resume():
    cmd = _agent_launch_flags("claude", "/bin/claude", "/w", "/p", session_id=SID, name="task 1: fix")
    assert f"--session-id {SID}" in cmd and f"-n {shlex.quote('task 1: fix')}" in cmd
    assert "--resume" not in cmd


def test_later_turn_resumes_and_never_renames():
    cmd = _agent_launch_flags("claude", "/bin/claude", "/w", "/p", session_id=SID, resume=True, name="x")
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
    c = RemoteScriptConnector(host="h", user="u", key_file="/k", repo_base="/repos", claude_binary="/bin/claude")
    c._config_expiry = float("inf")  # no DB refresh
    return c


async def test_ensure_task_worktree_is_idempotent_and_creates_on_branch(monkeypatch):
    c = _connector(); ex = _Exec(); monkeypatch.setattr(c, "_exec", ex)
    out = await c.ensure_task_worktree("acme/app", "/repos/acme/app-aegis-wt/task-1", "aegis-task/1")
    assert out["status"] == "ready"
    joined = "\n".join(ex.cmds)
    assert "worktree add -b aegis-task/1" in joined and "worktree add /repos/acme/app-aegis-wt/task-1 aegis-task/1" in joined
    assert "[ -d /repos/acme/app-aegis-wt/task-1 ]" in joined


async def test_start_run_with_caller_worktree_skips_provisioning_and_never_removes_it(monkeypatch):
    c = _connector()
    ex = _Exec({"cat > ": {"status": "failed", "exit_code": 1, "stdout": "", "stderr": "disk full"}})
    monkeypatch.setattr(c, "_exec", ex)
    out = await c.start_kimi_run(repo="acme/app", prompt="hi", kimi_binary="", engine_override="claude",
                                 worktree_path="/repos/acme/app-aegis-wt/task-1", session_id=SID)
    assert out["status"] == "failed"
    joined = "\n".join(ex.cmds)
    assert "worktree add" not in joined and "worktree remove" not in joined and "git pull" not in joined


async def test_kill_run_uses_fuser(monkeypatch):
    c = _connector(); ex = _Exec(); monkeypatch.setattr(c, "_exec", ex)
    assert await c.kill_run("/tmp/aegis-kimi-run-1.jsonl") is True
    assert "fuser -k /tmp/aegis-kimi-run-1.jsonl" in ex.cmds[-1]
```

- [ ] **Step 2: Run, see them fail. Implement. Run `tests/core/test_remote_script_task_sessions.py`, `tests/core/test_coding_session_inventory.py`, `tests/core/test_stop_coding_run.py`, `tests/core/test_kimi_connector.py`; ruff; commit** — `feat(connector): session flags, caller-owned worktrees, ensure_task_worktree, kill_run`

### Task 3: pure collision helpers in `coding_sessions.py`

**Files:**
- Modify: `core/src/aegis/connectors/coding_sessions.py` (append)
- Test: `tests/core/test_coding_sessions_same_task.py`

**Interfaces (all pure):**
- `find_session(sessions: list[dict], session_id: str) -> dict | None` — record whose `session_id` equals the argument (any status, any owner).
- `human_sessions_in_repo(sessions: list[dict], repo: str) -> list[dict]` — `owner == "human"` and `repo == target`, ANY status (unlike `match_busy`).
- `build_same_task_prompt(title: str, description: str, sessions: list[dict]) -> str` — each session rendered with `name`, `cwd`, `branch`, `log`, `status_short` (the git fields may be missing). Asks for one JSON object `{"same_task": bool, "session_name": str, "reason": str}` and nothing else.
- `parse_same_task_verdict(text: str) -> dict` — extracts the first `{...}` JSON object; on any failure returns `{"same_task": False, "session_name": "", "reason": "unparseable"}`; `same_task` is coerced with `bool`.

- [ ] **Step 1: Tests**

```python
from aegis.connectors.coding_sessions import (
    build_same_task_prompt, find_session, human_sessions_in_repo, parse_same_task_verdict)

S = [
    {"session_id": "a", "owner": "human", "repo": "acme/app", "status": "idle", "name": "fix eps"},
    {"session_id": "b", "owner": "aegis", "repo": "acme/app", "status": "busy", "name": "task 9"},
    {"session_id": "c", "owner": "human", "repo": "acme/web", "status": "busy", "name": "web"},
]

def test_find_session_matches_any_owner_or_status():
    assert find_session(S, "b")["name"] == "task 9"
    assert find_session(S, "zzz") is None

def test_human_sessions_in_repo_includes_idle_and_excludes_aegis():
    assert [s["session_id"] for s in human_sessions_in_repo(S, "acme/app")] == ["a"]
    assert human_sessions_in_repo(S, "") == []

def test_parse_verdict_extracts_json_and_fails_closed_to_false():
    v = parse_same_task_verdict('Sure.\n{"same_task": true, "session_name": "fix eps", "reason": "same branch"}')
    assert v == {"same_task": True, "session_name": "fix eps", "reason": "same branch"}
    assert parse_same_task_verdict("no json here")["same_task"] is False
    assert parse_same_task_verdict('{"same_task": "yes"}')["same_task"] is True

def test_prompt_names_every_session_and_asks_for_json():
    p = build_same_task_prompt("Fix EPS", "dupes", [{"name": "fix eps", "cwd": "/r/app", "branch": "fix/eps", "log": "abc fix", "status_short": " M a.py"}])
    assert "fix eps" in p and "fix/eps" in p and '"same_task"' in p
```

- [ ] **Step 2: Implement, run, ruff, commit** — `feat(core): same-task collision helpers`

### Task 4: worker activities for the task lane

**Files:**
- Modify: `worker/src/aegis_worker/activities/agent_task.py` — add fields `llm_client: Any = None`, `model_balanced: str = ""`; add activities below; DELETE `run_task_investigation`, `run_task_implementation`, `collect_coding_run`; add `AND NOT EXISTS (SELECT 1 FROM task_sessions ts WHERE ts.task_id = t.id)` to `find_actionable_tasks`.
- Modify: `worker/src/aegis_worker/activities/agent_run.py` — `check_agent_run` returns an extra `final` key (see below); add `_final_result_text(raw: str) -> str`.
- Modify: `worker/src/aegis_worker/__main__.py` — after `agent_task_act` is built: `agent_task_act.llm_client = deps.llm` and `agent_task_act.model_balanced = model_balanced` (the tier-resolved local that already exists in `main()`; grep `model_balanced`).
- Delete: `tests/worker/test_agent_task_skip.py`, `tests/worker/activities/test_agent_task_coding_collect.py`.
- Test: `tests/worker/activities/test_agent_task_sessions.py` (real DB for `find_task_turns_due`/`load_task`/`record_task_turn`; fakes for the rest), extend `tests/worker/test_agent_run_activities.py` (or the file holding `test_check_finished_when_process_exited_with_output`) with one test for `final`.

**Interfaces (new `@activity.defn` methods on `AgentTaskActivities`):**
- `load_task(task_id) -> dict` — `{id, content, description, labels, source_tag, project_id, assignee_label, notes}` where `notes` is the last 30 `todoist_notes` rows for the task, oldest first, each `{content, posted_at: iso str}`. Empty dict when the task is unknown.
- `ensure_task_session(task_id, agent_id, task, comment) -> dict` — `{"status": "ready"|"candidates"|"unresolved", "session": dict|None, "candidates": list}`. Creates the row via `create_session`. If the row has a repo → `ready`. Else run `resolve_task_repo(task)`; if `comment` matches a candidate (`github_repo`, `resource_title` or `resource_path`, case-insensitive exact) treat that candidate as resolved; a resolved repo → `set_repo` (`worktree_path = f"{repo_base}/{repo_path}-aegis-wt/task-{task_id}"` using `coding_settings()["repo_base"]`, `branch = f"aegis-task/{task_id}"`, `host = coding_settings()["host"]`) → `remote_script.ensure_task_worktree(...)`; a `failed` worktree → `unresolved` with the error in `"error"`. Candidates but no match → `candidates`.
- `check_task_collision(task_id, repo, session_id, override: bool) -> dict` — `{"verdict": "proceed"|"you_are_in_it"|"hand_to_you", "session": dict|None, "sessions": list, "reason": str}`. Inventory via `remote_script.list_coding_sessions()`; non-`ok` → `proceed`. `find_session` hit → `you_are_in_it`. Else `human_sessions_in_repo`; empty → `proceed`; `override` → `proceed` with the sessions listed. Else one SSH call per session via `remote_script.run_on_host(host, f"git -C {q(cwd)} branch --show-current; git -C {q(cwd)} log -3 --oneline; git -C {q(cwd)} status --short | head -20", timeout=20)`, split into `branch`/`log`/`status_short` (first line / next three / rest), then `llm_client.think(build_same_task_prompt(...), model=self.model_balanced or "balanced", max_tokens=4096, purpose="task_session_collision", agent_id=...)`; `parse_same_task_verdict(result["content"])`. `same_task` → `hand_to_you` with `session` = the named session (by name; first human session when the name does not match). No `llm_client` → `proceed`. Any exception → `proceed`.
- `launch_task_turn(session: dict, prompt: str, agent_id: str, resume: bool, name: str, turn_timeout_minutes: int) -> dict` — same return shape as `AgentRunActivities.launch_agent_run` (`status running|failed`, `run_id`, `output_file`, `host`, `engine`, `tmux_window`, `worktree_path`, `error`). Calls `remote_script.start_kimi_run(repo=session["repo"], prompt=prompt, kimi_binary=settings["kimi_binary"], github_repo=session["github_repo"], engine_override="claude", agent_id=agent_id, session_id=session["session_id"], resume=resume, name=name, worktree_path=session["worktree_path"], token_ttl_seconds=turn_timeout_minutes*60+3600)`.
- `kill_task_turn(output_file, host) -> dict` — `{"killed": bool}` via `remote_script.kill_run`.
- `record_task_turn(task_id, launched: bool) -> dict` — wraps `task_sessions.record_turn`.
- `find_task_turns_due(limit: int) -> list[dict]` — wraps `task_sessions.find_turns_due`.
- `_final_result_text(raw)` in `agent_run.py`: last stream-json line with `"type": "result"` and a string `result` → that string; else `""`. `check_agent_run` adds `"final": _final_result_text(raw)` on the `finished` branch and `"final": ""` elsewhere.

- [ ] **Step 1: Tests** — for the DB-backed ones seed `todoist_tasks`/`todoist_notes`/`task_sessions` as in Task 1; for `check_task_collision` use a fake connector class exposing `list_coding_sessions`, `run_on_host`, `coding_settings` and a fake `llm_client` with `async def think(self, prompt, **kw): return {"content": self.reply}`. Cover: `you_are_in_it` beats everything; no human sessions → `proceed`; LLM says same → `hand_to_you` naming the session; `override=True` → `proceed` even when the LLM would say same (assert `think` was NOT called); LLM raising → `proceed`. For `ensure_task_session`: ready row short-circuits (resolver not called); candidates without a matching comment → `candidates` and the row exists with empty repo; a comment naming a candidate → `ready`, `set_repo` applied, `ensure_task_worktree` called with `.../-aegis-wt/task-<id>` and `aegis-task/<id>`. For `_final_result_text`: two result lines → the last one's text; no result line → `""`.

- [ ] **Step 2: Implement, delete the three old activities and their two test files, wire `__main__.py`, run `tests/worker/activities/` and `tests/worker/test_agent_run*.py` and `tests/worker/test_agent_task_*.py`; ruff; commit** — `feat(worker): task-session activities; drop one-shot investigation/implementation`

### Task 5: extract `poll_until_exit` from `AgentRunFlow`

**Files:**
- Modify: `worker/src/aegis_worker/flows/agent_run.py:174-278`
- Test: existing `tests/worker/test_agent_run_flow.py` must pass unchanged.

**Interfaces:**
- Module-level `async def poll_until_exit(*, output_file: str, host: str, deadline_s: int, launched_at) -> dict` returning `{"status": "finished"|"failed"|"timeout", "output": str, "final": str, "reason": str, "elapsed_s": int}`. It contains the whole `while True` loop (the deadline check, the bounded sleep, the `check_agent_run` call with `schedule_to_close_timeout=remaining_s`, the first-poll `probe_alive=False`, the clock re-read). `AgentRunFlow.run` calls it and keeps its own deliver/cleanup/`_result` handling for each status.

- [ ] **Step 1: Refactor. Run `tests/worker/test_agent_run_flow.py` and `tests/worker/test_agent_run_skip.py`; ruff; commit** — `refactor(worker): extract poll_until_exit from AgentRunFlow`

### Task 6: `AgentTaskFlow` coding path, `comment` signal, sweep fallback

**Files:**
- Modify: `worker/src/aegis_worker/flows/agent_task.py` — `AgentTaskFlowInput` gains `comment: str = ""` and `turn_timeout_minutes: int = 60`; `AgentTaskSweepConfig` defaults `max_coding: int = 3`, new `turn_timeout_minutes: int = 60`; DELETE `_run_coding`, `_confirm_repo_gate0`, `_investigate_coding_task`, `_implement_and_open_pr` and the `InteractionFlow`/`_build_repo_confirm_prompt` imports if unused; keep `_park_coding`.
- Modify: `worker/src/aegis_worker/registry.py` only if the `schedule_config` builder for `AgentTaskSweepFlow` enumerates keys (add `turn_timeout_minutes`).
- Test: rewrite `tests/worker/flows/test_agent_task_coding.py`.

**Flow contract:**

```python
@workflow.defn(name="AgentTaskFlow")
class AgentTaskFlow:
    def __init__(self) -> None:
        self._pending: list[str] = []

    @workflow.signal
    def comment(self, text: str) -> None:
        text = (text or "").strip()
        if text and text not in self._pending:
            self._pending.append(text)

    def _drain(self) -> list[str]:
        out, self._pending = self._pending, []
        return out
```

`run`: when `input.task` is empty, `task = await execute_activity("load_task", ...)`; an empty result → comment "task not found" is impossible (no task), so just return `{"status": "unknown_task"}`. Then the existing verb dispatch; `coding` → `_run_coding`.

`_run_coding(input, task_id)` (every activity call uses the timeouts/retry policies the file already uses: `load_task`/`record_task_turn`/`find_task_turns_due` TIMEOUT_FAST+ACT_RETRY; `ensure_task_session`/`check_task_collision` TIMEOUT_STANDARD+ACT_RETRY; `launch_task_turn` TIMEOUT_LONG+NO_RETRY; `kill_task_turn` TIMEOUT_STANDARD+STANDARD; `comment` TIMEOUT_STANDARD+NO_RETRY; `park_task` TIMEOUT_FAST+ACT_RETRY):

```
comments = [input.comment.strip()] if input.comment.strip() else []
comments += self._drain()
turns_run = 0
while True:
    override = any(c.lower().startswith("take over") for c in comments)
    ensured = ensure_task_session(task_id, agent_id, task, comments[-1] if comments else "")
    if ensured.status == "candidates":
        record_task_turn(False); return _park_coding(task_id, "repo ambiguous", status="repo_ambiguous",
            comment="I can't tell which repository this is about. Reply with one of: " + ", ".join(c.github_repo for c in candidates), agent_id=...)
    if ensured.status == "unresolved":
        record_task_turn(False); return _park_coding(task_id, "repo unresolved", comment="I couldn't work out which repository this task is about" + (f": {error}" if error else "") + ", so I haven't touched anything.", ...)
    sess = ensured.session
    verdict = check_task_collision(task_id, sess.repo, sess.session_id, override)
    if verdict.verdict == "you_are_in_it":
        record_task_turn(False)
        DeliveryActivities.send_message(agent_id, f"You're in the session for task {task_id} ({verdict.session.name}); your comment is waiting for you there.")   # TIMEOUT_FAST, STANDARD, failure swallowed
        return {"task_id", "verb": "coding", "status": "operator_in_session"}      # NO park
    if verdict.verdict == "hand_to_you":
        record_task_turn(False)
        return _park_coding(task_id, "operator already on it", status="handed_to_operator",
            comment=f"You look to be on this already in session '{name}'" + (f" on branch `{branch}`" if branch else "") + ". I'll stay out. Reply `take over` when you want me to proceed.", ...)
    first = sess.turns == 0
    prompt = _first_turn_prompt(task_id, task, sess, notes=task.get("notes")) if first else _later_turn_prompt(task_id, task, sess, comments)
    record_task_turn(task_id, True)
    launched = launch_task_turn(sess, prompt, agent_id, resume=not first, name=f"task {task_id}: {title[:60]}", turn_timeout_minutes)
    if launched.status != "running":
        return _park_coding(task_id, "turn failed to start", status="launch_failed", comment=f"I couldn't start a turn on this: {launched.error}", ...)
    result = poll_until_exit(output_file=launched.output_file, host=launched.host, deadline_s=turn_timeout_minutes*60, launched_at=workflow.now())
    if result.status == "timeout":
        kill_task_turn(launched.output_file, launched.host)
        body = f"Turn timed out after {turn_timeout_minutes} min and was stopped.\n\n{result.output[-3000:]}"
    else:
        body = result.final or result.output[-6000:] or result.reason or "no output"
    if verdict.sessions:   # unrelated human sessions in the repo
        body = f"FYI: you have a live session in this repo ('{verdict.sessions[0].name}'); I'm working in my own worktree at {sess.worktree_path}.\n\n" + body
    body += f"\n\nSession: {sess.session_id} · turn {sess.turns + 1}\nTake over: cd {sess.worktree_path} && claude --resume {sess.session_id}"
    comment(task_id, agent_id, body)
    turns_run += 1
    comments = self._drain()
    if not comments: break
    sess = None  # re-read on the next loop iteration via ensure_task_session
return _park_coding(task_id, "waiting on you", status="parked", turns=turns_run, session_id=sess_id)
```

Prompts are module-level functions returning EXACTLY the two texts in spec section 3 (`_first_turn_prompt` renders the thread as `[<posted_at>] <content[:800]>` lines; `_later_turn_prompt` joins comments with blank lines, each line of a comment prefixed `> `).

Sweep: after the existing spawn loop, `due = execute_activity("find_task_turns_due", args=[config.max_coding], ...)`; for each: `try: start_child_workflow(AgentTaskFlow.run, AgentTaskFlowInput(agent_id=d.agent_id, todoist_task_id=d.task_id, comment=d.comment, turn_timeout_minutes=config.turn_timeout_minutes), id=f"agent-task-{d.task_id}", parent_close_policy=ABANDON) except WorkflowAlreadyStartedError: await workflow.get_external_workflow_handle(f"agent-task-{d.task_id}").signal("comment", d.comment)` — both wrapped so one failure logs and continues. Result gains `"resumed": n`.

- [ ] **Step 1: Rewrite the flow test.** Fake activities: `load_task`, `ensure_task_session`, `check_task_collision`, `record_task_turn`, `launch_task_turn`, `check_agent_run` (registered under `AgentRunActivities.check_agent_run`'s name, returning `finished` with `final="Plan: dedupe the rows\nSTATUS: plan"` on the second call), `kill_task_turn`, `comment`, `park_task`, `send_message` (DeliveryActivities). Register both `AgentTaskFlow` and (for the poll) nothing else — `poll_until_exit` is plain code. Tests:
  1. `test_first_turn_posts_plan_with_footer_and_parks` — events: `record(True)`, `launch(resume=False, name startswith "task tc-1:")`, `comment` containing "STATUS: plan", `Session: `, `Take over: cd`, then `park("waiting on you")`.
  2. `test_comment_signal_during_turn_runs_a_second_turn` — start the workflow, signal `comment("also fix tests")` while the first poll is pending (use an `asyncio.Event` in the fake `check_agent_run`), assert two launches, the second with `resume=True` and a prompt containing "> also fix tests", and exactly one park at the end.
  3. `test_operator_in_session_sends_slack_note_and_does_not_park` — verdict `you_are_in_it`: no `comment`, no `park`, `send_message` called, `record(False)`.
  4. `test_hand_to_you_comments_and_parks_without_launching` — verdict `hand_to_you`: comment contains "Reply `take over`", park, no launch.
  5. `test_take_over_comment_passes_override` — input comment "take over, go ahead": `check_task_collision` receives `override=True`.
  6. `test_timeout_kills_and_reports` — `check_agent_run` always `running`, `turn_timeout_minutes=1`, time-skipping: `kill_task_turn` called, comment contains "timed out".
  7. `test_ambiguous_repo_lists_candidates_and_parks` — `ensure_task_session` returns `candidates`: comment lists both repos, park, no launch.
  8. `test_sweep_dispatches_due_turns_by_start_or_signal` — sweep with `find_task_turns_due` returning one row while a child with that id is already running: assert the child received the signal (its fake records the comment).

- [ ] **Step 2: Implement; run `tests/worker/flows/`, `tests/worker/test_agent_task_*.py`, `tests/worker/test_cleanup.py`; ruff; commit** — `feat(worker): comment-driven task sessions in AgentTaskFlow`

### Task 7: webhook fast path and ClarifyFlow exclusion

**Files:**
- Modify: `core/src/aegis/api/routes/webhooks.py:~740-800` (inside the `note:added`/`note:updated` branch, after the `last_note_at` bump, before the ClarifyFlow kick)
- Modify: `worker/src/aegis_worker/activities/clarify.py` `find_unclassified_items` query: add `AND NOT EXISTS (SELECT 1 FROM task_sessions ts WHERE ts.task_id = t.id)` next to the other `NOT EXISTS` guards.
- Test: `tests/core/test_webhooks_todoist_task_turn.py` (copy the fixture from `test_webhooks_todoist_note.py`; give the mock pool `pool.fetchrow = AsyncMock(...)`), one DB test in `tests/worker/activities/test_agent_task_sessions.py` proving an Inbox task with a session row is not returned by `find_unclassified_items`.

**Webhook addition:**

```python
if item_id and not is_clarify_own and settings.temporal_host:
    try:
        sess = await pool.fetchrow("SELECT agent_id FROM task_sessions WHERE task_id = $1", str(item_id))
        if sess and is_user_note(content):
            client = await _Client.connect(settings.temporal_host)
            outcome = await dispatch_task_turn(client, task_id=str(item_id), agent_id=sess["agent_id"], comment=content)
            logger.info("todoist_webhook_task_turn_dispatched", item_id=str(item_id)[:32], outcome=outcome)
    except Exception as exc:  # noqa: BLE001 — the sweep fallback catches a missed dispatch
        logger.warning("todoist_webhook_task_turn_failed", error=str(exc)[:200])
```

Tests: a `note:added` for an item WITH a session row starts `AgentTaskFlow` with id `agent-task-<item>` and `comment` = the note (and still kicks ClarifyFlow); an item WITHOUT a row starts only ClarifyFlow; a note starting with `[Agent reply @ ` starts neither.

- [ ] **Step 1: Tests, implement, run `tests/core/test_webhooks_todoist*.py` and `tests/worker/activities/test_clarify_eligibility*.py` (whatever exists) ; ruff; commit** — `feat(core): dispatch a task turn from the Todoist note webhook`

- [ ] **Step 2: PR 1 gate.** Run all three package suites the CI way. `ruff check` per package. Update `docs/how-it-works.md` section 5 (replace the coding row of the verb table and the mermaid `@code` branch with: `@code → task session: turn per comment → plan → implement on branch when asked → draft PR when asked → @waiting`). Commit `docs: task sessions in how-it-works`. Push, open PR 1 titled `feat: comment-driven task sessions for the coding lane` with the spec linked.

---

## PR 2 — Slack threads, operator tool, housekeeping

### Task 8: core admin routes for task threads

**Files:**
- Create: `core/src/aegis/api/routes/task_sessions.py` — `APIRouter(prefix="/api/admin", dependencies=[Depends(verify_auth)])`
- Modify: `core/src/aegis/api/app.py` — import + `include_router`
- Test: `tests/core/test_task_sessions_routes.py` (ASGITransport + real `db_pool`, pattern from other route tests)

**Interfaces:**
- `GET /api/admin/task-sessions/by-thread?channel=&ts=` → `{"task_id": str|None}`
- `POST /api/admin/tasks/{task_id}/comment` body `{"text": str}` → `{"ok": bool, "task_id"}`; 404 when no `task_sessions` row; writes a plain note via `TodoistConnector(api_key=await resolve_todoist_api_key(pool, settings), db_pool=pool, timeout=10.0)` with `build_note_add_command` + `commands` + `check_sync_status` exactly like `_exec_mark_waiting`; 503 "Todoist not configured" when no key. The text is stored verbatim (no prefix, no footer).

- [ ] Tests (monkeypatch `TodoistConnector.commands` to record the command and return an ok envelope; `check_sync_status` may be patched to return `{}`), implement, run, ruff, commit — `feat(core): task-session thread lookup and comment routes`

### Task 9: comms — thread delivery and threaded replies

**Files:**
- Modify: `comms/src/aegis_comms/__main__.py` — `DeliveryRequest.thread_ref: dict | None = None`; `deliver()` passes `target={"channel": ref["channel"], "thread_ts": ref["ts"]}` when set.
- Modify: `comms/src/aegis_comms/adapters/slack.py` — `send_message(..., target)` honours `target["thread_ts"]` (pass `thread_ts=` to `chat_postMessage` for every chunk); `_on_message` passes `thread_ts=event.get("thread_ts", "")`.
- Modify: `comms/src/aegis_comms/slack_inbound.py` — `SlackCoreClient.task_by_thread(channel, ts) -> str | None` (GET) and `task_comment(task_id, text) -> bool` (POST); `on_message(..., thread_ts: str = "")`: after the bot/self/empty guards and before the note-to-self check, `if thread_ts and thread_ts != ts: task_id = await self._core.task_by_thread(channel_id, thread_ts); if task_id: await self._core.task_comment(task_id, text); return`.
- Test: extend `tests/comms/test_slack_inbound.py` (threaded reply on a task thread → `task_comment` called, no chat route; unknown thread → normal routing; a top-level message with `thread_ts == ts` → normal routing) and `tests/comms/test_slack_adapter_outbound.py` (thread_ts forwarded) and `tests/comms/test_delivery.py` (thread_ref accepted).

- [ ] Tests, implement, run `tests/comms/`, ruff, commit — `feat(comms): per-task Slack threads`

### Task 10: worker — deliver task messages into the thread

**Files:**
- Modify: `worker/src/aegis_worker/activities/delivery.py` — `send_message(agent_id, message, chat_id=0, thread_ref: dict | None = None)` forwards `thread_ref` in the JSON body and returns the comms response (which carries `delivery_ref`).
- Modify: `worker/src/aegis_worker/activities/agent_task.py` — `set_task_slack_ref(task_id, ref: dict) -> dict`.
- Modify: `worker/src/aegis_worker/flows/agent_task.py` — after every `comment(...)` in `_run_coding` and in the `hand_to_you`/`candidates` branches: `resp = send_message(agent_id, text, thread_ref=sess.slack_ref)`; when `sess.slack_ref` was None and `resp.get("delivery_ref")` exists → `set_task_slack_ref(task_id, resp["delivery_ref"])`. The root message text is `f"Task {task_id}: {title}\n\n{body}"`. The `you_are_in_it` note also goes to the thread when one exists. Delivery failures are swallowed (log only).
- Test: extend the flow test — first turn stores the returned ref; second turn sends with `thread_ref` set.

- [ ] Tests, implement, run, ruff, commit — `feat(worker): deliver task turns into the task's Slack thread`

### Task 11: `comment_on_task` chat tool

**Files:**
- Modify: `core/src/aegis/services/tools/gtd.py` — `@aegis_tool async def _exec_comment_on_task(pool, ctx, *, task_id: str, text: str) -> str` with a docstring that becomes the tool description: "Post a comment in your own voice on a Todoist task. On a task with a coding session this starts the session's next turn. Args: task_id: the Todoist task id. text: the comment, posted verbatim." Body mirrors `_exec_mark_waiting` (`resolve_todoist_api_key`, `TodoistConnector`, `build_note_add_command(task_id, text)`, `commands`, `check_sync_status`, `_stage_chat_tool_outbox(pool, [cmd], status, "comment_on_task")`), returns `f"Commented on {task_id}"`.
- Modify: `core/src/aegis/services/chat.py` — import; `_registry_schema("comment_on_task")` right after `_registry_schema("handoff_task")` in `CHAT_TOOLS`; `"comment_on_task": _exec_comment_on_task` next to `"handoff_task"` in `TOOL_EXECUTORS`; add `comment_on_task` to the sebas and pandoras-actor entries of `AGENT_TOOL_SETS` (seed-time default only). Hand-write both edits; do NOT `ruff format` this file; verify `git diff main -- core/src/aegis/services/chat.py | grep -c '^@@'` is ≤ 4.
- Modify: `core/src/aegis/api/routes/mcp_server.py` — add `"comment_on_task"` to `_UNSERVED_TOOLS` with a one-line comment: a run commenting on its own task would trigger its own next turn.
- Modify: `tests/core/fixtures/chat_tools_golden.json` (insert the schema object after `handoff_task`, surgically) and `EXPECTED_TOOL_NAMES` in `tests/core/test_chat_tool_registry.py` (alphabetical position).
- Test: `tests/core/test_chat_tool_comment_on_task.py` — executor posts a `note_add` command with the verbatim text and no prefix; refuses empty text; the parametrised unserved test in `test_mcp_server_endpoint.py` picks the new member up automatically (run it).

- [ ] Tests, implement, run `tests/core/test_chat_tool*.py`, `tests/core/test_mcp_server*.py`, ruff, commit — `feat(core): comment_on_task tool, operator mount only`

### Task 12: CleanupFlow worktree sweep and docs

**Files:**
- Modify: `worker/src/aegis_worker/activities/cleanup.py` — `CleanupActivities` gains `remote_script: Any = None` and `@activity.defn async def cleanup_task_sessions(self, days: int = 7) -> dict` → `{"removed": n, "skipped": m}`: rows where `(t.is_completed OR t.id IS NULL)` via `LEFT JOIN todoist_tasks t ON t.id = ts.task_id` and `COALESCE(ts.last_turn_at, ts.created_at) < now() - make_interval(days => $1)`; for each: `remote_script.remove_worktree(worktree_path, host=host)` when `worktree_path` is non-empty, then `DELETE FROM task_sessions WHERE task_id = $1`.
- Modify: `worker/src/aegis_worker/flows/cleanup.py` — a step after the orphan sweep, same try/except shape, `result["task_sessions"] = ...`; config key `task_session_days: int = 7` on the cleanup config dataclass.
- Modify: `worker/src/aegis_worker/__main__.py` — pass `remote_script=connectors.get("remote_script")` to `CleanupActivities(...)`.
- Modify: `docs/infrastructure.md` — under the coding block section, add the two config keys and the `comment_on_task` grant SQL:
  `UPDATE agents SET metadata = jsonb_set(metadata,'{tool_set}',(metadata->'tool_set')||'["comment_on_task"]'::jsonb) WHERE active AND metadata ? 'tool_set' AND NOT (metadata->'tool_set' @> '["comment_on_task"]'::jsonb);`
- Test: `tests/worker/test_cleanup_task_sessions.py` (real DB: an aged completed task's row is removed and `remove_worktree` called; an open task's row survives; a young completed row survives).

- [ ] Tests, implement, run `tests/worker/test_cleanup*.py`, ruff, commit — `feat(worker): sweep finished task-session worktrees` ; then push and open PR 2 `feat: task threads on Slack, comment_on_task, worktree sweep`.

---

## Rollout (after both PRs merge and the release is deployed)

1. Infra row `meem`, `coding` block: `routing.default_engine = "claude"`, `engines.claude.default_account = "personal"` — via `PUT /api/admin/infra/{id}` with the FULL coding object (the validator replaces it).
2. `activities.config` for `agent-task-15min`: `{"max_coding": 3, "turn_timeout_minutes": 60}`.
3. Grant `comment_on_task` (SQL above) to the active agents.
4. Comment on a parked `@code` task and walk the loop: plan comment + Slack root, threaded reply → implement turn, `claude --resume` takeover → rule 1 fires, `take over`.
5. `workflow_runs`: `result_summary->>'verb' = 'coding'` rows with `turns`/`session_id`.
