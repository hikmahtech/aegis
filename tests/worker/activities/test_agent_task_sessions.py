"""Task-lane activities: session bootstrap, collision lookup, turn launch.

The DB-backed tests run against the real test database (`work_sessions`,
`todoist_tasks`, `todoist_notes`). Everything that would reach the coding host
or an LLM uses a fake whose signature is pinned to the real class at the bottom
of this file — a fake that has drifted from the class it stands in for is the
one way these tests could pass while production is broken.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid

import pytest_asyncio
from aegis.services import work_sessions as svc
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
    await db_pool.execute("DELETE FROM work_sessions WHERE task_id = $1", _TASK)
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

    def __init__(
        self,
        *,
        sessions=None,
        worktree="ready",
        git_stdout="",
        launch="running",
        git_delay=0.0,
        git_error=False,
        alive=True,
    ):
        self.sessions = sessions if sessions is not None else []
        self.worktree = worktree
        self.git_stdout = git_stdout
        self.launch = launch
        self.git_delay = git_delay
        self.git_error = git_error
        # "boom" raises, mirroring an SSH probe that blew up rather than
        # answering — which the activity must read as "not one of ours".
        self.alive = alive
        self.worktree_calls: list[dict] = []
        self.launches: list[dict] = []
        self.git_calls: list[dict] = []
        self.killed: list[dict] = []
        self.alive_calls: list[dict] = []

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
        if self.git_error:
            raise RuntimeError("ssh probe exploded")
        if self.git_delay:
            await asyncio.sleep(self.git_delay)
        return {"status": "succeeded", "exit_code": 0, "stdout": self.git_stdout, "stderr": ""}

    async def kimi_run_alive(self, output_file: str, host: str = "") -> bool:
        self.alive_calls.append({"output_file": output_file, "host": host})
        if self.alive == "boom":
            raise RuntimeError("ssh probe exploded")
        return bool(self.alive)

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
                "claude_account": claude_account,
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
            # The label the connector resolved the launch to — what the row
            # records and the next turn resumes under.
            "claude_account": claude_account or "work",
        }


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


async def test_record_task_turn_reports_a_missing_session_row(db_pool, _task):
    """The row can be cleaned up while a turn is running. Claiming a watermark
    that does not exist would hide why the same comment keeps coming back."""
    assert await AgentTaskActivities(db_pool=db_pool).record_task_turn(_TASK, True) == {
        "recorded": False
    }


async def test_record_task_turn_without_a_pool_reports_not_recorded():
    assert await AgentTaskActivities(db_pool=None).record_task_turn(_TASK, True) == {
        "recorded": False
    }


# --- set_task_slack_ref ------------------------------------------------------


async def test_set_task_slack_ref_remembers_the_thread_root(db_pool, _task):
    """Stored as a jsonb OBJECT, not a JSON string. Every later message posts
    under `slack_ref->>'ts'` and inbound routing matches on it, so a
    double-encoded value would silently open a fresh thread on every turn and
    lose the reply route with it."""
    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    act = AgentTaskActivities(db_pool=db_pool)

    assert await act.set_task_slack_ref(_TASK, {"channel": "C1", "ts": "1.1"}) == {"stored": True}
    assert (await svc.get_session(db_pool, _TASK))["slack_ref"] == {"channel": "C1", "ts": "1.1"}


async def test_set_task_slack_ref_refuses_an_empty_ref(db_pool, _task):
    """An empty ref would overwrite a real root with one that matches no
    thread — worse than never storing one."""
    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    act = AgentTaskActivities(db_pool=db_pool)
    await act.set_task_slack_ref(_TASK, {"channel": "C1", "ts": "1.1"})

    assert await act.set_task_slack_ref(_TASK, {}) == {"stored": False}
    assert (await svc.get_session(db_pool, _TASK))["slack_ref"] == {"channel": "C1", "ts": "1.1"}


async def test_set_task_slack_ref_without_a_pool_stores_nothing():
    assert await AgentTaskActivities(db_pool=None).set_task_slack_ref(
        _TASK, {"channel": "C1", "ts": "1.1"}
    ) == {"stored": False}


# --- ensure_task_session -----------------------------------------------------


async def _ready_row(db_pool) -> None:
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


async def test_ready_row_skips_the_resolver_but_still_verifies_its_worktree(db_pool, _task):
    """A resolved session must never re-resolve: the repo is settled, and
    re-running the resolver every turn would let a later LLM guess move the task
    to a different checkout mid-conversation.

    The worktree IS re-checked, because it can vanish under us — a manual
    `git worktree remove`, a disk clean — and the check is an idempotent no-op
    when it is there."""
    await _ready_row(db_pool)
    conn = _Connector()
    act = AgentTaskActivities(db_pool=db_pool, remote_script=conn)
    calls: list = []
    act.resolve_task_repo = _resolver(_RESOLVED, calls)

    out = await act.ensure_task_session(_TASK, "pandoras-actor", {"id": _TASK}, "")

    assert out["status"] == "ready"
    assert out["session"]["repo"] == "hikmah/aegis"
    assert out["session"]["worktree_path"] == _WT
    assert calls == []
    assert conn.worktree_calls == [
        {"repo": "hikmah/aegis", "worktree_path": _WT, "branch": _BRANCH, "host": "meem"}
    ]


async def test_a_ready_row_whose_worktree_cannot_be_rebuilt_is_unresolved(db_pool, _task):
    """Falsifiability pair with the test above: same ready row, and the only
    difference is a worktree that will not build. Reporting `ready` there would
    launch the turn into a directory that is not on the host."""
    await _ready_row(db_pool)
    conn = _Connector(worktree="failed")
    act = AgentTaskActivities(db_pool=db_pool, remote_script=conn)
    act.resolve_task_repo = _resolver(_RESOLVED, [])

    out = await act.ensure_task_session(_TASK, "pandoras-actor", {"id": _TASK}, "")

    assert out["status"] == "unresolved"
    assert "checkout missing" in out["error"]


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

_OPERATOR_SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_OUTPUT = "/tmp/aegis-kimi-run-r1.jsonl"


def _collision_act(db_pool, conn):
    return AgentTaskActivities(db_pool=db_pool, remote_script=conn)


async def _own_session_row(db_pool, *, output_file: str = "", host: str = "meem") -> None:
    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    if output_file:
        await svc.set_last_run(db_pool, _TASK, output_file=output_file, host=host)


async def _operator_row(db_pool, *, status: str = "active", account: str = "personal") -> dict:
    return await svc.upsert_operator_session(
        db_pool,
        task_id=_TASK,
        account=account,
        status=status,
        summary="halfway through the retry cap",
        session_id=_OPERATOR_SID,
    )


async def test_nothing_on_the_task_proceeds(db_pool, _task):
    await _own_session_row(db_pool)
    conn = _Connector(alive=True)
    out = await _collision_act(db_pool, conn).check_task_collision(_TASK, False)
    assert out["verdict"] == "proceed"
    assert conn.alive_calls == [], "nothing to probe without a recorded run"


async def test_our_own_orphan_turn_is_still_running(db_pool, _task):
    """Rule 1. The last turn AEGIS launched is still holding its output file
    open — an orphan the deadline kill did not reach. A `--resume` beside it
    would have two runs writing one session."""
    await _own_session_row(db_pool, output_file=_OUTPUT)
    conn = _Connector(alive=True)
    out = await _collision_act(db_pool, conn).check_task_collision(_TASK, False)
    assert out["verdict"] == "turn_still_running"
    assert out["session"]["owner"] == "aegis"
    assert out["session"]["session_id"] == (await svc.get_session(db_pool, _TASK))["session_id"]
    assert conn.alive_calls == [{"output_file": _OUTPUT, "host": "meem"}]


async def test_a_finished_turn_is_not_an_orphan(db_pool, _task):
    await _own_session_row(db_pool, output_file=_OUTPUT)
    out = await _collision_act(db_pool, _Connector(alive=False)).check_task_collision(_TASK)
    assert out["verdict"] == "proceed"


async def test_an_unprobeable_turn_proceeds(db_pool, _task):
    """A probe that raised is "not running": the launch that follows fails on
    its own terms if the host really is down, and a comment must not be held
    back for ever by a probe that cannot answer."""
    await _own_session_row(db_pool, output_file=_OUTPUT)
    out = await _collision_act(db_pool, _Connector(alive="boom")).check_task_collision(_TASK)
    assert out["verdict"] == "proceed"


async def test_an_active_operator_session_means_you_are_in_it(db_pool, _task):
    """Rule 2. The operator's own session reported itself on the task with
    `report_progress`, so the comment is already in front of them — and the
    verdict names the session so the Slack note can."""
    await _own_session_row(db_pool)
    await _operator_row(db_pool)
    out = await _collision_act(db_pool, _Connector(alive=False)).check_task_collision(_TASK)
    assert out["verdict"] == "you_are_in_it"
    assert out["session"]["owner"] == "operator"
    assert out["session"]["account"] == "personal"
    assert out["session"]["session_id"] == _OPERATOR_SID
    assert out["session"]["name"] == "halfway through the retry cap"
    assert "personal" in out["reason"]


async def test_a_parked_or_stale_operator_session_proceeds(db_pool, _task):
    await _own_session_row(db_pool)
    await _operator_row(db_pool, status="parked")
    out = await _collision_act(db_pool, _Connector()).check_task_collision(_TASK)
    assert out["verdict"] == "proceed"

    await _operator_row(db_pool, status="active")
    await db_pool.execute(
        "UPDATE work_sessions SET last_seen_at = now() - interval '2 hours' "
        "WHERE task_id = $1 AND owner = 'operator'",
        _TASK,
    )
    out = await _collision_act(db_pool, _Connector()).check_task_collision(_TASK)
    assert out["verdict"] == "proceed", "an active row past the window is not a person in the task"


async def test_take_over_overrides_the_operator_row_but_not_an_orphan(db_pool, _task):
    """Rule 4. `take over` is the operator overruling their own registry row —
    but it cannot authorise driving over a turn of ours that is still running."""
    await _own_session_row(db_pool)
    await _operator_row(db_pool)
    out = await _collision_act(db_pool, _Connector()).check_task_collision(_TASK, True)
    assert out["verdict"] == "proceed" and out["reason"] == "override"

    await svc.set_last_run(db_pool, _TASK, output_file=_OUTPUT, host="meem")
    out = await _collision_act(db_pool, _Connector(alive=True)).check_task_collision(_TASK, True)
    assert out["verdict"] == "turn_still_running"


async def test_the_orphan_check_beats_the_operator_row(db_pool, _task):
    await _own_session_row(db_pool, output_file=_OUTPUT)
    await _operator_row(db_pool)
    out = await _collision_act(db_pool, _Connector(alive=True)).check_task_collision(_TASK)
    assert out["verdict"] == "turn_still_running"


async def test_no_pool_proceeds():
    out = await AgentTaskActivities(remote_script=_Connector()).check_task_collision(_TASK)
    assert out == {"verdict": "proceed", "session": None, "reason": "no database pool"}


async def test_a_broken_registry_read_proceeds(_task):
    class _BoomPool:
        async def fetchrow(self, *a, **k):
            raise RuntimeError("connection reset")

    out = await AgentTaskActivities(db_pool=_BoomPool(), remote_script=None).check_task_collision(
        _TASK
    )
    assert out["verdict"] == "proceed"
    assert out["reason"].startswith("check failed: ")


# --- reconcile_work_sessions -------------------------------------------------


async def _seen_ago(db_pool, account: str, interval: str) -> None:
    await db_pool.execute(
        "UPDATE work_sessions SET last_seen_at = now() - $3::text::interval "
        "WHERE task_id = $1 AND owner = 'operator' AND account = $2",
        _TASK,
        account,
        interval,
    )


async def _operator_status(db_pool, account: str) -> str:
    return await db_pool.fetchval(
        "SELECT status FROM work_sessions WHERE task_id = $1 AND owner = 'operator' "
        "AND account = $2",
        _TASK,
        account,
    )


async def test_reconcile_parks_stale_operator_rows_the_host_does_not_list(db_pool, _task):
    """`report_progress` said active; `claude agents --json` says whether the
    session still exists. Listed → touched. Unlisted and quiet past the window
    → parked, so `task_context` stops showing a session that ended without a
    final report."""
    live = await _operator_row(db_pool, account="personal")
    gone = await svc.upsert_operator_session(
        db_pool,
        task_id=_TASK,
        account="work",
        status="active",
        summary="x",
        session_id="bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee",
    )
    await _seen_ago(db_pool, "personal", "2 hours")
    await _seen_ago(db_pool, "work", "2 hours")
    conn = _Connector(sessions=[dict(_HUMAN, session_id=live["session_id"])])
    out = await AgentTaskActivities(db_pool=db_pool, remote_script=conn).reconcile_work_sessions()
    assert out == {"refreshed": 1, "parked": 1, "inventory": "ok"}
    assert await _operator_status(db_pool, "personal") == "active"
    assert await _operator_status(db_pool, "work") == "parked"
    seen = await db_pool.fetchval(
        "SELECT last_seen_at > now() - interval '1 minute' FROM work_sessions WHERE id = $1::uuid",
        live["id"],
    )
    assert seen, "a listed session's row is touched"
    assert gone["status"] == "active", "the row was active before the sweep"


async def test_reconcile_leaves_a_recent_unlisted_row_alone(db_pool, _task):
    """The hook may not know the session id, so an unlisted row is only stale
    once it has also gone quiet — a fresh report is proof enough on its own."""
    await _operator_row(db_pool)
    out = await AgentTaskActivities(db_pool=db_pool, remote_script=_Connector()).reconcile_work_sessions()
    assert out == {"refreshed": 0, "parked": 0, "inventory": "ok"}
    assert await _operator_status(db_pool, "personal") == "active"


async def test_reconcile_fails_open_without_an_inventory(db_pool, _task):
    await _operator_row(db_pool)
    await _seen_ago(db_pool, "personal", "2 hours")
    for conn in (_Connector(sessions="unavailable"), _Connector(sessions="boom"), None):
        out = await AgentTaskActivities(db_pool=db_pool, remote_script=conn).reconcile_work_sessions()
        assert out["parked"] == 0 and out["inventory"] != "ok", conn
    assert await _operator_status(db_pool, "personal") == "active"
    out = await AgentTaskActivities(remote_script=_Connector()).reconcile_work_sessions()
    assert out["inventory"] == "no database pool"


_HUMAN = {
    "account": "personal",
    "session_id": _OPERATOR_SID,
    "name": "fix-retry",
    "cwd": "/w/hikmah/aegis",
    "repo": "hikmah/aegis",
    "status": "idle",
    "kind": "",
    "owner": "human",
}


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


async def test_a_running_launch_records_where_the_turn_writes(db_pool, _task):
    """`check_task_collision` probes this file to tell an orphan of ours from an
    operator takeover, so a launch that does not record it makes every takeover
    look like an orphan.

    Falsifiable: drop the `set_last_run` call and both columns stay empty.
    """
    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    conn = _Connector()
    act = AgentTaskActivities(db_pool=db_pool, remote_script=conn)
    out = await act.launch_task_turn(_SESSION, "investigate", "pandoras-actor", False, "t", 60)

    assert out["status"] == "running"
    row = await svc.get_session(db_pool, _TASK)
    assert row["last_output_file"] == "/tmp/aegis-kimi-run-r1.jsonl"
    assert row["last_host"] == "meem"


async def test_a_running_launch_records_the_account_it_resolved(db_pool, _task):
    """The row remembers the CLAUDE_CONFIG_DIR label the connector picked, so
    the next turn's `--resume` runs under the same profile. An empty label
    (the host's default login) keeps whatever the row had."""
    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    await svc.set_repo(
        db_pool, _TASK, repo="hikmah/aegis", github_repo="hikmahtech/aegis",
        worktree_path=_WT, branch=_BRANCH, host="meem",
    )
    act = AgentTaskActivities(db_pool=db_pool, remote_script=_Connector())
    await act.launch_task_turn(_SESSION, "investigate", "pandoras-actor", False, "t", 60)
    row = await svc.get_session(db_pool, _TASK)
    assert row["account"] == "work" and row["engine"] == "claude"
    assert row["status"] == "active"


async def test_a_later_turn_resumes_under_the_recorded_account():
    conn = _Connector()
    await AgentTaskActivities(remote_script=conn).launch_task_turn(
        dict(_SESSION, turns=1, account="personal"), "go", "pandoras-actor", True, "t", 30
    )
    assert conn.launches[0]["claude_account"] == "personal"
    assert conn.launches[0]["resume"] is True


async def test_a_failed_launch_records_no_run(db_pool, _task):
    """Nothing is writing, so nothing may claim to be: a stale output file left
    behind by a failed launch would read as a live orphan on the next turn."""
    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")
    act = AgentTaskActivities(db_pool=db_pool, remote_script=_Connector(launch="failed"))
    await act.launch_task_turn(_SESSION, "p", "pandoras-actor", False, "n", 60)

    row = await svc.get_session(db_pool, _TASK)
    assert row["last_output_file"] == "" and row["last_host"] == ""


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


# --- clarify hands the task over ---------------------------------------------


@pytest_asyncio.fixture(loop_scope="function")
async def _inbox(db_pool):
    """Put the fixture task in a managed Inbox so ClarifyActivities can see it,
    and put the settings row back afterwards — the test database is shared with
    every other file this xdist worker runs."""
    prior = await db_pool.fetchval(
        "SELECT value FROM settings WHERE key = 'todoist_managed_project_ids'"
    )
    await db_pool.execute(
        "INSERT INTO settings (key, value) VALUES ('todoist_managed_project_ids', $1) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        {"inbox": "PROJ-INBOX"},
    )
    await db_pool.execute(
        "INSERT INTO todoist_projects (id, name, is_managed, raw) "
        "VALUES ('PROJ-INBOX', 'Inbox', true, '{}'::jsonb) ON CONFLICT (id) DO NOTHING"
    )
    await db_pool.execute("UPDATE todoist_tasks SET project_id = 'PROJ-INBOX' WHERE id = $1", _TASK)
    yield
    if prior is None:
        await db_pool.execute("DELETE FROM settings WHERE key = 'todoist_managed_project_ids'")
    else:
        await db_pool.execute(
            "UPDATE settings SET value = $1 WHERE key = 'todoist_managed_project_ids'", prior
        )


async def test_clarify_hands_a_session_task_over_and_stops_looking_at_it(db_pool, _task, _inbox):
    """A task with a session row must drop out of clarify's eligibility query.

    Its comments are turns, and clarify's own answer to a fresh comment on an
    agent-labelled task is to spawn AgentChatReplyFlow. Without this exclusion
    one comment gets BOTH — a chat reply and a coding turn, two agents talking
    over each other on the same thread, and the reply's `[Agent reply @ ` note
    landing in the middle of the session's own transcript.

    The first half of the test is the control: the same task, same comment, no
    session row, IS returned. So a regression that removes the exclusion fails
    here rather than passing vacuously.
    """
    from aegis_worker.activities.clarify import ClarifyActivities

    await _note(db_pool, "use the other repo")
    acts = ClarifyActivities(db_pool=db_pool)

    assert _TASK in [r["id"] for r in await acts.find_unclassified_items(max_items=50)]

    await svc.create_session(db_pool, task_id=_TASK, agent_id="pandoras-actor")

    assert _TASK not in [r["id"] for r in await acts.find_unclassified_items(max_items=50)]


# --- fake/real contracts -----------------------------------------------------


def test_fake_connector_matches_the_real_signatures():
    """The fakes above CLAIM to mirror the real classes. Without this, a rename
    on either one leaves every test in this file passing against a fake that no
    longer resembles what production calls."""
    from aegis.connectors.remote_script import RemoteScriptConnector

    for name in (
        "coding_settings",
        "ensure_task_worktree",
        "list_coding_sessions",
        "run_on_host",
        "kill_run",
        "kimi_run_alive",
        "start_kimi_run",
    ):
        real = inspect.signature(getattr(RemoteScriptConnector, name))
        fake = inspect.signature(getattr(_Connector, name))
        assert list(fake.parameters) == list(real.parameters), name
