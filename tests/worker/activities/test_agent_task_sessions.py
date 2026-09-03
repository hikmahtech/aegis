"""Task-lane activities: session bootstrap, collision verdict, turn launch.

The DB-backed tests run against the real test database (`task_sessions`,
`todoist_tasks`, `todoist_notes`). Everything that would reach the coding host
or an LLM uses a fake whose signature is pinned to the real class at the bottom
of this file — a fake that has drifted from the class it stands in for is the
one way these tests could pass while production is broken.
"""

from __future__ import annotations

import inspect
import uuid

import pytest_asyncio
from aegis.services import task_sessions as svc
from aegis_worker.activities.agent_task import AgentTaskActivities

_TASK = "ats-1"
_SESSION_ID = "11111111-2222-3333-4444-555555555555"
_WT = f"/w/hikmah/aegis-aegis-wt/task-{_TASK}"
_BRANCH = f"aegis-task/{_TASK}"

_CANDIDATES = [
    {
        "resource_title": "AEGIS",
        "github_repo": "hikmahtech/aegis",
        "resource_path": "hikmah/aegis",
        "score": 0.6,
    },
    {
        "resource_title": "BCP",
        "github_repo": "acme/bcp",
        "resource_path": "acme/bcp",
        "score": 0.4,
    },
]

_RESOLVED = {
    "github_repo": "hikmahtech/aegis",
    "repo_path": "hikmah/aegis",
    "source": "project_map",
    "candidates": [],
}
_UNRESOLVED = {"github_repo": "", "repo_path": "", "source": "none", "candidates": _CANDIDATES}


async def _purge(db_pool) -> None:
    await db_pool.execute("DELETE FROM task_sessions WHERE task_id = $1", _TASK)
    await db_pool.execute("DELETE FROM todoist_notes WHERE item_id = $1", _TASK)
    await db_pool.execute("DELETE FROM todoist_tasks WHERE id = $1", _TASK)


@pytest_asyncio.fixture(loop_scope="function")
async def _task(db_pool):
    await _purge(db_pool)
    await db_pool.execute(
        "INSERT INTO todoist_tasks (id, content, description, labels, source_tag, "
        "assignee_label, is_completed, updated_at) "
        "VALUES ($1, 'Fix the retry policy', 'It retries forever.', "
        "ARRAY['@pandora','@code'], NULL, '@pandora', false, now())",
        _TASK,
    )
    yield
    await _purge(db_pool)


async def _note(db_pool, content: str, age: str = "0 seconds") -> None:
    # $4::text::interval, not $4::interval — a bare interval cast makes asyncpg
    # infer the parameter as an interval and demand a timedelta.
    await db_pool.execute(
        "INSERT INTO todoist_notes (id, item_id, content, posted_at, raw) "
        "VALUES ($1, $2, $3, now() - $4::text::interval, '{}')",
        str(uuid.uuid4()),
        _TASK,
        content,
        age,
    )


def _resolver(result: dict, calls: list):
    """Stand-in for `AgentTaskActivities.resolve_task_repo`, recording each call
    so a test can prove the resolver was NOT reached."""

    async def resolve(task: dict) -> dict:
        calls.append(task)
        return result

    return resolve


class _Connector:
    """Coding-host stand-in. Signatures mirror `RemoteScriptConnector` and are
    pinned by `test_fake_connector_matches_the_real_signatures` below."""

    def __init__(self, *, sessions=None, worktree="ready", git_stdout="", launch="running"):
        self.sessions = sessions if sessions is not None else []
        self.worktree = worktree
        self.git_stdout = git_stdout
        self.launch = launch
        self.worktree_calls: list[dict] = []
        self.launches: list[dict] = []
        self.git_calls: list[dict] = []
        self.killed: list[dict] = []

    async def coding_settings(self) -> dict:
        return {
            "host": "meem",
            "repo_base": "/w",
            "kimi_binary": "/bin/kimi",
            "claude_binary": "/bin/claude",
        }

    async def ensure_task_worktree(
        self, repo: str, worktree_path: str, branch: str, host: str = ""
    ) -> dict:
        self.worktree_calls.append(
            {"repo": repo, "worktree_path": worktree_path, "branch": branch, "host": host}
        )
        if self.worktree == "ready":
            return {"status": "ready", "error": ""}
        return {"status": "failed", "error": "Repo checkout missing on meem: /w/hikmah/aegis"}

    async def list_coding_sessions(self) -> dict:
        if self.sessions == "boom":
            raise RuntimeError("ssh exploded")
        if self.sessions == "unavailable":
            return {"status": "unavailable", "sessions": [], "errors": [], "skip_when_busy": True}
        return {
            "status": "ok",
            "sessions": list(self.sessions),
            "errors": [],
            "skip_when_busy": True,
        }

    async def run_on_host(
        self, host: str, remote_cmd: str, timeout: int = 30, stdin: bytes | None = None
    ) -> dict:
        self.git_calls.append({"host": host, "cmd": remote_cmd, "timeout": timeout})
        return {"status": "succeeded", "exit_code": 0, "stdout": self.git_stdout, "stderr": ""}

    async def kill_run(self, output_file: str, host: str = "") -> bool:
        self.killed.append({"output_file": output_file, "host": host})
        return True

    async def start_kimi_run(
        self,
        repo: str,
        prompt: str,
        kimi_binary: str,
        timeout: int = 1800,
        github_repo: str = "",
        engine_override: str = "",
        claude_config_dir: str = "",
        claude_account: str = "",
        agent_id: str = "",
        gated: bool = False,
        token_ttl_seconds: int = 0,
        session_id: str = "",
        resume: bool = False,
        name: str = "",
        worktree_path: str = "",
    ) -> dict:
        self.launches.append(
            {
                "repo": repo,
                "prompt": prompt,
                "kimi_binary": kimi_binary,
                "github_repo": github_repo,
                "engine_override": engine_override,
                "agent_id": agent_id,
                "token_ttl_seconds": token_ttl_seconds,
                "session_id": session_id,
                "resume": resume,
                "name": name,
                "worktree_path": worktree_path,
            }
        )
        if self.launch != "running":
            return {"status": "failed", "error": "no such checkout", "run_id": ""}
        return {
            "status": "running",
            "run_id": "r1",
            "output_file": "/tmp/aegis-kimi-run-r1.jsonl",
            "host": "meem",
            "engine": "claude",
            "in_tmux": True,
            "worktree_path": worktree_path,
        }


class _LLM:
    """`LLMClient.think` stand-in; `reply` may be an exception to raise."""

    def __init__(self, reply):
        self.reply = reply
        self.calls: list[dict] = []

    async def think(
        self,
        prompt: str,
        model: str = "gemma4:e2b",
        system_prompt: str | None = None,
        max_tokens: int = 2000,
        db_pool=None,
        purpose: str | None = None,
        agent_id: str | None = None,
    ) -> dict:
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "max_tokens": max_tokens,
                "purpose": purpose,
                "agent_id": agent_id,
            }
        )
        if isinstance(self.reply, Exception):
            raise self.reply
        return {"content": self.reply}


# --- load_task ---------------------------------------------------------------


async def test_load_task_returns_the_task_and_its_recent_notes(db_pool, _task):
    """The comment thread IS the prompt's context, so the notes ride along with
    the task, oldest first and capped at 30."""
    for n in range(32):
        await _note(db_pool, f"note {n}", age=f"{40 - n} minutes")
    task = await AgentTaskActivities(db_pool=db_pool).load_task(_TASK)
    assert task["id"] == _TASK
    assert task["content"] == "Fix the retry policy"
    assert task["description"] == "It retries forever."
    assert task["labels"] == ["@pandora", "@code"]
    assert task["assignee_label"] == "@pandora"
    assert [n["content"] for n in task["notes"]] == [f"note {n}" for n in range(2, 32)]
    # An activity result crosses Temporal's payload boundary — timestamps go as
    # ISO strings, never as datetimes.
    assert isinstance(task["notes"][0]["posted_at"], str)
    assert task["notes"][0]["posted_at"] < task["notes"][-1]["posted_at"]


async def test_load_task_unknown_task_is_empty(db_pool):
    assert await AgentTaskActivities(db_pool=db_pool).load_task("no-such-task") == {}


async def test_load_task_without_a_pool_is_empty():
    assert await AgentTaskActivities(db_pool=None).load_task(_TASK) == {}


# --- find_task_turns_due / record_task_turn ----------------------------------


async def test_find_task_turns_due_surfaces_an_unanswered_user_comment(db_pool, _task):
    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    await _note(db_pool, "use the other repo")
    due = await AgentTaskActivities(db_pool=db_pool).find_task_turns_due(10)
    assert [(d["task_id"], d["agent_id"], d["comment"]) for d in due] == [
        (_TASK, "pandoras-actor", "use the other repo")
    ]


async def test_find_task_turns_due_without_a_pool_is_empty():
    assert await AgentTaskActivities(db_pool=None).find_task_turns_due(10) == []


async def test_record_task_turn_counts_only_launched_turns(db_pool, _task):
    """Every verdict moves the watermark; only a launched turn is a turn."""
    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    act = AgentTaskActivities(db_pool=db_pool)

    assert (await act.record_task_turn(_TASK, False))["recorded"] is True
    row = await svc.get_session(db_pool, _TASK)
    assert row["turns"] == 0
    assert row["last_turn_at"] is not None

    await act.record_task_turn(_TASK, True)
    assert (await svc.get_session(db_pool, _TASK))["turns"] == 1


async def test_record_task_turn_without_a_pool_reports_not_recorded():
    assert await AgentTaskActivities(db_pool=None).record_task_turn(_TASK, True) == {
        "recorded": False
    }


# --- ensure_task_session -----------------------------------------------------


async def test_ready_row_short_circuits_the_resolver(db_pool, _task):
    """A resolved session must never re-resolve: the repo is settled, and
    re-running the resolver every turn would let a later LLM guess move the
    task to a different checkout mid-conversation."""
    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    await svc.set_repo(
        db_pool,
        _TASK,
        repo="hikmah/aegis",
        github_repo="hikmahtech/aegis",
        worktree_path=_WT,
        branch=_BRANCH,
        host="meem",
    )
    conn = _Connector()
    act = AgentTaskActivities(db_pool=db_pool, remote_script=conn)
    calls: list = []
    act.resolve_task_repo = _resolver(_RESOLVED, calls)

    out = await act.ensure_task_session(_TASK, "pandoras-actor", {"id": _TASK}, "")

    assert out["status"] == "ready"
    assert out["session"]["repo"] == "hikmah/aegis"
    assert out["session"]["worktree_path"] == _WT
    assert calls == []
    assert conn.worktree_calls == []


async def test_a_resolved_repo_needs_no_comment(db_pool, _task):
    conn = _Connector()
    act = AgentTaskActivities(db_pool=db_pool, remote_script=conn)
    act.resolve_task_repo = _resolver(_RESOLVED, [])

    out = await act.ensure_task_session(_TASK, "pandoras-actor", {"id": _TASK}, "")

    assert out["status"] == "ready"
    assert out["candidates"] == []
    assert conn.worktree_calls == [
        {"repo": "hikmah/aegis", "worktree_path": _WT, "branch": _BRANCH, "host": "meem"}
    ]
    row = await svc.get_session(db_pool, _TASK)
    assert (row["repo"], row["github_repo"], row["branch"], row["host"]) == (
        "hikmah/aegis",
        "hikmahtech/aegis",
        _BRANCH,
        "meem",
    )


async def test_candidates_without_a_matching_comment_park_the_row_empty(db_pool, _task):
    """The row still exists — that is what makes the NEXT comment reach the
    flow at all — but with no repo, so nothing is checked out on a guess."""
    conn = _Connector()
    act = AgentTaskActivities(db_pool=db_pool, remote_script=conn)
    act.resolve_task_repo = _resolver(_UNRESOLVED, [])

    out = await act.ensure_task_session(_TASK, "pandoras-actor", {"id": _TASK}, "which one?")

    assert out["status"] == "candidates"
    assert [c["github_repo"] for c in out["candidates"]] == ["hikmahtech/aegis", "acme/bcp"]
    row = await svc.get_session(db_pool, _TASK)
    assert row is not None
    assert row["repo"] == "" and row["worktree_path"] == "" and row["branch"] == ""
    assert conn.worktree_calls == []


async def test_a_comment_naming_a_candidate_resolves_it(db_pool, _task):
    """Case-insensitive and whitespace-tolerant: the operator types the repo
    back in a Todoist comment, not into a form."""
    conn = _Connector()
    act = AgentTaskActivities(db_pool=db_pool, remote_script=conn)
    act.resolve_task_repo = _resolver(_UNRESOLVED, [])

    out = await act.ensure_task_session(_TASK, "pandoras-actor", {"id": _TASK}, " HikmahTech/Aegis ")

    assert out["status"] == "ready"
    assert out["session"]["repo"] == "hikmah/aegis"
    assert out["session"]["github_repo"] == "hikmahtech/aegis"
    assert out["session"]["worktree_path"] == _WT
    assert out["session"]["branch"] == _BRANCH
    assert conn.worktree_calls == [
        {"repo": "hikmah/aegis", "worktree_path": _WT, "branch": _BRANCH, "host": "meem"}
    ]


async def test_a_candidate_can_be_named_by_title_or_path(db_pool, _task):
    conn = _Connector()
    act = AgentTaskActivities(db_pool=db_pool, remote_script=conn)
    act.resolve_task_repo = _resolver(_UNRESOLVED, [])

    out = await act.ensure_task_session(_TASK, "pandoras-actor", {"id": _TASK}, "bcp")

    assert out["status"] == "ready"
    assert out["session"]["repo"] == "acme/bcp"
    assert out["session"]["github_repo"] == "acme/bcp"


async def test_a_failed_worktree_leaves_the_row_unresolved(db_pool, _task):
    """The repo is NOT recorded when the worktree could not be built: a row
    carrying a repo short-circuits to `ready` for ever, and the next turn would
    launch into a directory that does not exist."""
    conn = _Connector(worktree="failed")
    act = AgentTaskActivities(db_pool=db_pool, remote_script=conn)
    act.resolve_task_repo = _resolver(_RESOLVED, [])

    out = await act.ensure_task_session(_TASK, "pandoras-actor", {"id": _TASK}, "")

    assert out["status"] == "unresolved"
    assert "checkout missing" in out["error"]
    row = await svc.get_session(db_pool, _TASK)
    assert row["repo"] == "" and row["worktree_path"] == ""


async def test_ensure_without_a_connector_is_unresolved(db_pool, _task):
    act = AgentTaskActivities(db_pool=db_pool, remote_script=None)
    act.resolve_task_repo = _resolver(_RESOLVED, [])
    out = await act.ensure_task_session(_TASK, "pandoras-actor", {"id": _TASK}, "")
    assert out["status"] == "unresolved"
    assert out["error"]


# --- check_task_collision ----------------------------------------------------

_HUMAN = {
    "account": "personal",
    "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "name": "fix-retry",
    "cwd": "/w/hikmah/aegis",
    "repo": "hikmah/aegis",
    "status": "idle",
    "kind": "",
    "owner": "human",
}
_OURS = {
    "account": "personal",
    "session_id": _SESSION_ID,
    "name": f"task {_TASK}: Fix the retry policy",
    "cwd": _WT,
    "repo": "hikmah/aegis",
    "status": "busy",
    "kind": "",
    "owner": "aegis",
}
_GIT = (
    "fix-retry\n"
    "abc1234 cap the retry policy\n"
    "def5678 add a failing test\n"
    "0011aab scaffold\n"
    " M worker/src/aegis_worker/activities/agent_task.py\n"
    "?? notes.md\n"
)
_SAME = '{"same_task": true, "session_name": "fix-retry", "reason": "same branch"}'


def _collision_act(db_pool, conn, llm=None, model_balanced="kimi-k2.5"):
    return AgentTaskActivities(
        db_pool=db_pool, remote_script=conn, llm_client=llm, model_balanced=model_balanced
    )


async def test_our_own_live_session_beats_every_other_verdict(db_pool, _task):
    """Rule 1. The operator has resumed THIS task's session, so the comment is
    already in front of them — asking an LLM anything would be wasted, and any
    other verdict would double-drive one conversation."""
    conn = _Connector(sessions=[_HUMAN, _OURS], git_stdout=_GIT)
    llm = _LLM(_SAME)
    out = await _collision_act(db_pool, conn, llm).check_task_collision(
        _TASK, "hikmah/aegis", _SESSION_ID, False
    )
    assert out["verdict"] == "you_are_in_it"
    assert out["session"]["name"] == _OURS["name"]
    assert llm.calls == []
    assert conn.git_calls == []


async def test_no_human_session_in_the_repo_proceeds(db_pool, _task):
    conn = _Connector(sessions=[dict(_HUMAN, owner="aegis")], git_stdout=_GIT)
    llm = _LLM(_SAME)
    out = await _collision_act(db_pool, conn, llm).check_task_collision(
        _TASK, "hikmah/aegis", _SESSION_ID, False
    )
    assert out["verdict"] == "proceed"
    assert out["sessions"] == []
    assert llm.calls == []


async def test_the_llm_hands_the_task_over_when_a_person_is_on_it(db_pool, _task):
    # The flow always ensures the session before it checks for a collision, so
    # the row is there and the LLM spend is attributed to the owning agent.
    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    conn = _Connector(sessions=[_HUMAN], git_stdout=_GIT)
    llm = _LLM(_SAME)
    out = await _collision_act(db_pool, conn, llm).check_task_collision(
        _TASK, "hikmah/aegis", _SESSION_ID, False
    )
    assert out["verdict"] == "hand_to_you"
    assert out["session"]["name"] == "fix-retry"
    assert out["session"]["branch"] == "fix-retry"
    assert out["reason"] == "same branch"

    # One SSH round trip per candidate session, and its output reaches the
    # prompt: "same repo" is not a collision, so branch/commits/dirty files are
    # the whole basis for the verdict.
    assert len(conn.git_calls) == 1
    assert conn.git_calls[0]["host"] == "meem"
    cmd = conn.git_calls[0]["cmd"]
    assert cmd.startswith("git -C /w/hikmah/aegis branch --show-current")
    assert "log -3 --oneline" in cmd and "status --short" in cmd
    prompt = llm.calls[0]["prompt"]
    assert "fix-retry" in prompt
    assert "cap the retry policy" in prompt
    assert "notes.md" in prompt
    assert "Fix the retry policy" in prompt  # the task title
    assert llm.calls[0]["purpose"] == "task_session_collision"
    assert llm.calls[0]["max_tokens"] == 4096
    assert llm.calls[0]["agent_id"] == "pandoras-actor"


async def test_the_session_working_directory_is_shell_quoted(db_pool, _task):
    """The cwd comes from `claude agents --json` on the coding host — a path
    AEGIS did not choose — and it is spliced into a remote shell command."""
    conn = _Connector(sessions=[dict(_HUMAN, cwd="/w/my repo; rm -rf x")], git_stdout=_GIT)
    await _collision_act(db_pool, conn, _LLM(_SAME)).check_task_collision(
        _TASK, "hikmah/aegis", _SESSION_ID, False
    )
    assert conn.git_calls[0]["cmd"].startswith("git -C '/w/my repo; rm -rf x' branch")


async def test_hand_to_you_falls_back_to_the_first_session_when_the_name_is_unknown(
    db_pool, _task
):
    conn = _Connector(sessions=[_HUMAN], git_stdout=_GIT)
    llm = _LLM('{"same_task": true, "session_name": "something else", "reason": "r"}')
    out = await _collision_act(db_pool, conn, llm).check_task_collision(
        _TASK, "hikmah/aegis", _SESSION_ID, False
    )
    assert out["verdict"] == "hand_to_you"
    assert out["session"]["name"] == "fix-retry"


async def test_an_unrelated_human_session_proceeds_but_is_reported(db_pool, _task):
    """Rule 3: the turn runs, and the sessions come back so the flow can warn
    the operator that it is working alongside them."""
    conn = _Connector(sessions=[_HUMAN], git_stdout=_GIT)
    llm = _LLM('{"same_task": false, "session_name": "", "reason": "different feature"}')
    out = await _collision_act(db_pool, conn, llm).check_task_collision(
        _TASK, "hikmah/aegis", _SESSION_ID, False
    )
    assert out["verdict"] == "proceed"
    assert [s["name"] for s in out["sessions"]] == ["fix-retry"]


async def test_take_over_skips_the_same_task_check(db_pool, _task):
    """Rule 4. The override must not merely ignore a `hand_to_you` verdict — it
    must not ASK, or a model that keeps saying "same task" would keep costing a
    call the operator has already overruled."""
    conn = _Connector(sessions=[_HUMAN], git_stdout=_GIT)
    llm = _LLM(_SAME)
    out = await _collision_act(db_pool, conn, llm).check_task_collision(
        _TASK, "hikmah/aegis", _SESSION_ID, True
    )
    assert out["verdict"] == "proceed"
    assert llm.calls == []
    assert conn.git_calls == []
    assert [s["name"] for s in out["sessions"]] == ["fix-retry"]


async def test_a_failing_llm_proceeds(db_pool, _task):
    conn = _Connector(sessions=[_HUMAN], git_stdout=_GIT)
    llm = _LLM(RuntimeError("model is down"))
    out = await _collision_act(db_pool, conn, llm).check_task_collision(
        _TASK, "hikmah/aegis", _SESSION_ID, False
    )
    assert out["verdict"] == "proceed"


async def test_no_llm_client_proceeds(db_pool, _task):
    conn = _Connector(sessions=[_HUMAN], git_stdout=_GIT)
    out = await _collision_act(db_pool, conn, None).check_task_collision(
        _TASK, "hikmah/aegis", _SESSION_ID, False
    )
    assert out["verdict"] == "proceed"


async def test_a_broken_inventory_proceeds(db_pool, _task):
    for sessions in ("boom", "unavailable"):
        conn = _Connector(sessions=sessions)
        out = await _collision_act(db_pool, conn, _LLM(_SAME)).check_task_collision(
            _TASK, "hikmah/aegis", _SESSION_ID, False
        )
        assert out["verdict"] == "proceed", sessions


async def test_no_connector_proceeds(db_pool, _task):
    out = await _collision_act(db_pool, None, _LLM(_SAME)).check_task_collision(
        _TASK, "hikmah/aegis", _SESSION_ID, False
    )
    assert out["verdict"] == "proceed"


async def test_an_empty_model_balanced_resolves_through_the_tier_map(db_pool, _task):
    """`balanced` is a TIER, not a model name. Sending the literal upstream is a
    guaranteed 404 on a path that then fails open, so the collision check would
    silently stop working."""
    from aegis.llm.tier import set_model_tiers

    previous = set_model_tiers({"balanced": "kimi-k2.5"})
    try:
        conn = _Connector(sessions=[_HUMAN], git_stdout=_GIT)
        llm = _LLM(_SAME)
        act = _collision_act(db_pool, conn, llm, model_balanced="")
        await act.check_task_collision(_TASK, "hikmah/aegis", _SESSION_ID, False)
        assert llm.calls[0]["model"] == "kimi-k2.5"
    finally:
        set_model_tiers(previous)


# --- launch_task_turn / kill_task_turn ---------------------------------------

_SESSION = {
    "task_id": _TASK,
    "agent_id": "pandoras-actor",
    "session_id": _SESSION_ID,
    "repo": "hikmah/aegis",
    "github_repo": "hikmahtech/aegis",
    "worktree_path": _WT,
    "branch": _BRANCH,
    "host": "meem",
}


async def test_launch_pins_the_turn_to_the_tasks_session_and_worktree():
    """Every flag here is load-bearing: without `session_id` the turn has no
    memory of the last one, and without `worktree_path` the connector would
    provision (and later remove) a throwaway worktree instead."""
    conn = _Connector()
    act = AgentTaskActivities(remote_script=conn)
    out = await act.launch_task_turn(_SESSION, "investigate", "pandoras-actor", False, "task 1", 60)

    call = conn.launches[0]
    assert call["repo"] == "hikmah/aegis"
    assert call["github_repo"] == "hikmahtech/aegis"
    assert call["engine_override"] == "claude"
    assert call["agent_id"] == "pandoras-actor"
    assert call["session_id"] == _SESSION_ID
    assert call["resume"] is False
    assert call["name"] == "task 1"
    assert call["worktree_path"] == _WT
    assert call["token_ttl_seconds"] == 60 * 60 + 3600
    assert call["prompt"] == "investigate"

    assert out["status"] == "running"
    assert out["run_id"] == "r1"
    assert out["output_file"] == "/tmp/aegis-kimi-run-r1.jsonl"
    assert out["host"] == "meem"
    assert out["engine"] == "claude"
    assert out["worktree_path"] == _WT
    assert out["tmux_window"] == "claude-aegis-r1"
    assert out["error"] == ""


async def test_a_later_turn_resumes_the_same_session():
    conn = _Connector()
    await AgentTaskActivities(remote_script=conn).launch_task_turn(
        _SESSION, "and now implement", "pandoras-actor", True, "task 1", 30
    )
    call = conn.launches[0]
    assert call["resume"] is True
    assert call["session_id"] == _SESSION_ID
    assert call["token_ttl_seconds"] == 30 * 60 + 3600


async def test_a_failed_launch_reports_the_error():
    conn = _Connector(launch="failed")
    out = await AgentTaskActivities(remote_script=conn).launch_task_turn(
        _SESSION, "p", "pandoras-actor", False, "n", 60
    )
    assert out["status"] == "failed"
    assert "no such checkout" in out["error"]


async def test_launch_without_a_connector_fails():
    out = await AgentTaskActivities(remote_script=None).launch_task_turn(
        _SESSION, "p", "pandoras-actor", False, "n", 60
    )
    assert out["status"] == "failed"
    assert out["error"]


async def test_kill_task_turn_targets_the_output_file():
    conn = _Connector()
    out = await AgentTaskActivities(remote_script=conn).kill_task_turn("/tmp/o.jsonl", "meem")
    assert out == {"killed": True}
    assert conn.killed == [{"output_file": "/tmp/o.jsonl", "host": "meem"}]


async def test_kill_task_turn_without_a_connector_or_file():
    assert await AgentTaskActivities(remote_script=None).kill_task_turn("/tmp/o", "meem") == {
        "killed": False
    }
    assert await AgentTaskActivities(remote_script=_Connector()).kill_task_turn("", "meem") == {
        "killed": False
    }


# --- fake/real contracts -----------------------------------------------------


def test_fake_connector_matches_the_real_signatures():
    """The fakes above CLAIM to mirror the real classes. Without this, a rename
    on either one leaves every test in this file passing against a fake that no
    longer resembles what production calls."""
    from aegis.connectors.remote_script import RemoteScriptConnector
    from aegis.llm import LLMClient

    for name in (
        "coding_settings",
        "ensure_task_worktree",
        "list_coding_sessions",
        "run_on_host",
        "kill_run",
        "start_kimi_run",
    ):
        real = inspect.signature(getattr(RemoteScriptConnector, name))
        fake = inspect.signature(getattr(_Connector, name))
        assert list(fake.parameters) == list(real.parameters), name

    real_think = inspect.signature(LLMClient.think)
    fake_think = inspect.signature(_LLM.think)
    assert list(fake_think.parameters) == list(real_think.parameters)
    kwargs = {"prompt": "p", "model": "m", "max_tokens": 10, "purpose": "x", "agent_id": "a"}
    real_think.bind(None, **kwargs)
    fake_think.bind(None, **kwargs)
