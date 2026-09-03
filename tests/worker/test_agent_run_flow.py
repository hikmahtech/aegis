"""AgentRunFlow — happy path, non-retried launch failure, and timeout.

The flow is the heavy lane: launch a headless CLI run, poll it, deliver the
transcript tail to the agent's channel. The three cases below lock the three
behaviours that are expensive to get wrong — a delivered result, a launch that
is attempted exactly ONCE (a retry is a second billed CLI session), and a
deadline that reports where the still-running process is instead of killing it.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from aegis_worker.activities.agent_run import AgentRunActivities
from aegis_worker.flows.agent_run import AgentRunFlow, AgentRunInput
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


def _launched(**over) -> dict:
    base = {
        "status": "running",
        "repo": "aegis-scratch",
        "run_id": "ab12cd34",
        "output_file": "/tmp/aegis-kimi-run-ab12cd34.jsonl",
        "host": "node-a",
        "engine": "claude",
        "in_tmux": True,
        "tmux_window": "claude-scratch-ab12cd34",
        "worktree_path": "/w/aegis-scratch-aegis-wt/ab12cd34",
    }
    base.update(over)
    return base


def _cleanup_activity(calls: list[dict]):
    """Stand-in for `cleanup_agent_run`, recording what the flow asked to remove.

    Registered by every workflow test below: the flow calls it on each terminal
    path, and an unregistered activity would turn into retries and noise rather
    than a clear failure."""

    @activity.defn(name="cleanup_agent_run")
    async def cleanup(worktree_path, output_file="", host=""):
        calls.append({"worktree_path": worktree_path, "output_file": output_file, "host": host})
        return {"removed": True, "reason": ""}

    return cleanup


@pytest.mark.asyncio
async def test_happy_path_delivers_output_tail():
    """Launch ok → first check finished → transcript tail delivered, status ok."""
    launch_calls: list[dict] = []
    check_calls: list[dict] = []
    cleanup_calls: list[dict] = []
    delivered: list[str] = []
    transcript = "Read 14 files.\n\nSUMMARY: the retry policy is unbounded."

    @activity.defn(name="launch_agent_run")
    async def launch(
        prompt, repo="", engine="", purpose="", agent_id="", gated=False, timeout_minutes=0
    ):
        launch_calls.append(
            {
                "prompt": prompt,
                "repo": repo,
                "engine": engine,
                "purpose": purpose,
                "agent_id": agent_id,
                "gated": gated,
            }
        )
        return _launched()

    @activity.defn(name="check_agent_run")
    async def check(output_file, host="", probe_alive=True):
        check_calls.append({"output_file": output_file, "probe_alive": probe_alive})
        return {"status": "finished", "output": transcript, "reason": ""}

    @activity.defn(name="send_message")
    async def send_message(agent_id, message, chat_id=0, keyboard=None):
        delivered.append(message)
        return {"ok": True, "message_id": 42}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"tq-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunFlow],
            activities=[launch, check, send_message, _cleanup_activity(cleanup_calls)],
        ):
            result = await env.client.execute_workflow(
                AgentRunFlow.run,
                AgentRunInput(
                    agent_id="pandoras-actor",
                    prompt="Audit the retry policy in bcp.",
                    purpose="retry audit",
                ),
                id=f"arf-happy-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result["status"] == "ok"
    assert result["run_id"] == "ab12cd34"
    assert result["engine"] == "claude"
    assert result["host"] == "node-a"
    assert result["elapsed_s"] > 0
    # Uniform result shape across every return path.
    assert set(result) == {"status", "reason", "run_id", "engine", "host", "elapsed_s"}

    # repo omitted ⇒ the scratch checkout, not an empty path. agent_id must
    # reach the launch: it is what mounts that agent's AEGIS tools into the run
    # (dropping it yields a silently toolless run, not an error).
    assert launch_calls == [
        {
            "prompt": "Audit the retry policy in bcp.",
            "repo": "aegis-scratch",
            "engine": "",
            "purpose": "retry audit",
            "agent_id": "pandoras-actor",
            "gated": False,
        }
    ]
    # First poll must NOT probe liveness — SSH/checkout latency would read as
    # a dead run and end a healthy one at 30 seconds.
    assert check_calls[0]["probe_alive"] is False
    assert check_calls[0]["output_file"] == "/tmp/aegis-kimi-run-ab12cd34.jsonl"

    # The run's worktree is removed once the run is over (#300). No `output_file`
    # on this path: the poll already saw the process gone, so re-probing would
    # only add a fail-open window in which a dead run's worktree survives.
    assert cleanup_calls == [
        {"worktree_path": "/w/aegis-scratch-aegis-wt/ab12cd34", "output_file": "", "host": "node-a"}
    ]

    assert len(delivered) == 1
    assert transcript in delivered[0]
    assert "retry audit" in delivered[0]
    assert "run=ab12cd34" in delivered[0]
    assert "engine=claude" in delivered[0]


@pytest.mark.asyncio
async def test_launch_failure_is_attempted_once_and_delivered():
    """A launch retry is a SECOND billed CLI session on a second worktree, so
    the launch activity must run exactly once and the failure be delivered.

    Falsifiable: swap NO_RETRY for STANDARD/RETRY_ONCE in the flow and
    `attempts == 1` fails."""
    attempts = {"count": 0}
    delivered: list[str] = []
    cleanup_calls: list[dict] = []
    checked = {"hit": False}

    @activity.defn(name="launch_agent_run")
    async def launch(
        prompt, repo="", engine="", purpose="", agent_id="", gated=False, timeout_minutes=0
    ):
        attempts["count"] += 1
        raise RuntimeError("ssh: connect to host node-a port 22: Connection refused")

    @activity.defn(name="check_agent_run")
    async def check(output_file, host="", probe_alive=True):
        checked["hit"] = True  # MUST NOT FIRE — nothing was launched
        return {"status": "running", "output": "", "reason": ""}

    @activity.defn(name="send_message")
    async def send_message(agent_id, message, chat_id=0, keyboard=None):
        delivered.append(message)
        return {"ok": True, "message_id": 1}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"tq-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunFlow],
            activities=[launch, check, send_message, _cleanup_activity(cleanup_calls)],
        ):
            result = await env.client.execute_workflow(
                AgentRunFlow.run,
                AgentRunInput(agent_id="sebas", prompt="do a thing", purpose="thing"),
                id=f"arf-launchfail-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert attempts["count"] == 1, "launch must be attempted exactly once (NO_RETRY)"
    assert checked["hit"] is False
    # Nothing launched ⇒ no worktree to remove. The connector cleans up its own
    # half-built launches; the flow must not invent a path it never received.
    assert cleanup_calls == []
    assert result["status"] == "failed"
    assert "Connection refused" in (result["reason"] or "")
    assert set(result) == {"status", "reason", "run_id", "engine", "host", "elapsed_s"}
    assert len(delivered) == 1
    assert "Connection refused" in delivered[0]


@pytest.mark.asyncio
async def test_missing_scratch_checkout_delivers_provision_command():
    """The connector's hard failure on a missing checkout is only fixable by
    provisioning it — the delivered message has to say how."""
    delivered: list[str] = []
    cleanup_calls: list[dict] = []

    @activity.defn(name="launch_agent_run")
    async def launch(
        prompt, repo="", engine="", purpose="", agent_id="", gated=False, timeout_minutes=0
    ):
        return {
            "status": "failed",
            "error": "Repo checkout missing on node-a: /w/aegis-scratch — provision it",
            "repo": "aegis-scratch",
            "repo_base": "/w",
            "run_id": "zz99",
            "engine": "kimi",
        }

    @activity.defn(name="check_agent_run")
    async def check(output_file, host="", probe_alive=True):
        return {"status": "running", "output": "", "reason": ""}

    @activity.defn(name="send_message")
    async def send_message(agent_id, message, chat_id=0, keyboard=None):
        delivered.append(message)
        return {"ok": True, "message_id": 1}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"tq-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunFlow],
            activities=[launch, check, send_message, _cleanup_activity(cleanup_calls)],
        ):
            result = await env.client.execute_workflow(
                AgentRunFlow.run,
                AgentRunInput(agent_id="sebas", prompt="analyse this"),
                id=f"arf-noscratch-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result["status"] == "failed"
    assert len(delivered) == 1
    assert "git init" in delivered[0]
    assert "/w/aegis-scratch" in delivered[0]


@pytest.mark.asyncio
async def test_timeout_fires_at_the_deadline_and_reports_where_the_run_is():
    """Deadline reached with the run still alive: report the tmux window and
    the output file, return status timeout, and DON'T stop the process.

    `timeout_minutes=5` with an upper bound on `elapsed_s` is the part that
    matters — the old assertion was `elapsed_s >= 60` against a 1-minute
    deadline, which cannot tell "fired on time" from "fired eventually", and a
    prod run sailed 2h17m past a 30-minute deadline while this test stayed
    green (#295).
    """
    delivered: list[str] = []
    checks = {"count": 0}
    cleanup_calls: list[dict] = []

    @activity.defn(name="launch_agent_run")
    async def launch(
        prompt, repo="", engine="", purpose="", agent_id="", gated=False, timeout_minutes=0
    ):
        return _launched(engine="kimi", tmux_window="kimi-scratch-ab12cd34")

    @activity.defn(name="check_agent_run")
    async def check(output_file, host="", probe_alive=True):
        checks["count"] += 1
        return {"status": "running", "output": "", "reason": ""}

    @activity.defn(name="send_message")
    async def send_message(agent_id, message, chat_id=0, keyboard=None):
        delivered.append(message)
        return {"ok": True, "message_id": 1}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"tq-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunFlow],
            activities=[launch, check, send_message, _cleanup_activity(cleanup_calls)],
        ):
            result = await env.client.execute_workflow(
                AgentRunFlow.run,
                AgentRunInput(
                    agent_id="sebas",
                    prompt="long job",
                    purpose="long job",
                    timeout_minutes=5,
                ),
                id=f"arf-timeout-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result["status"] == "timeout"
    assert result["run_id"] == "ab12cd34"
    # 5 simulated minutes, not "at least one minute, whenever": the deadline is
    # a deadline. The slack is for activity execution, which is real time even
    # under time skipping.
    assert 300 <= result["elapsed_s"] <= 330, result["elapsed_s"]
    assert set(result) == {"status", "reason", "run_id", "engine", "host", "elapsed_s"}
    # The run kept being polled the whole way (not abandoned after one look).
    assert checks["count"] >= 5
    # Cleanup is attempted here too, but WITH the output file: the process was
    # deliberately not stopped, so the activity's liveness probe — not the flow —
    # decides whether the worktree can go. Passing "" here would delete the cwd
    # of a run that is still writing to it.
    assert cleanup_calls == [
        {
            "worktree_path": "/w/aegis-scratch-aegis-wt/ab12cd34",
            "output_file": "/tmp/aegis-kimi-run-ab12cd34.jsonl",
            "host": "node-a",
        }
    ]
    assert len(delivered) == 1
    msg = delivered[0]
    assert "still running" in msg
    assert "kimi-scratch-ab12cd34" in msg
    assert "/tmp/aegis-kimi-run-ab12cd34.jsonl" in msg
    assert "NOT been stopped" in msg


@pytest.mark.asyncio
async def test_the_deadline_holds_when_the_polls_themselves_are_slow():
    """The #295 shape: the flow's time goes INSIDE the poll, not between polls.

    Prod's poll stalled and the loop only ever compared the deadline against a
    clock sample taken *before* the activity — so the deadline check ran late,
    against a stale number, and the run went 2h17m past a 30-minute budget.
    Here each poll burns 10 real seconds against a 60-second deadline.

    Falsifiable: sample `elapsed` once at the top of the iteration and reuse it
    after the poll (the old code) and this reports ~70s instead of ~60s.
    Remove the `schedule_to_close_timeout` cap as well and a poll that never
    returns pushes it arbitrarily far.
    """
    delivered: list[str] = []
    cleanup_calls: list[dict] = []

    @activity.defn(name="launch_agent_run")
    async def launch(
        prompt, repo="", engine="", purpose="", agent_id="", gated=False, timeout_minutes=0
    ):
        return _launched()

    @activity.defn(name="check_agent_run")
    async def check(output_file, host="", probe_alive=True):
        await asyncio.sleep(10)
        return {"status": "running", "output": "", "reason": ""}

    @activity.defn(name="send_message")
    async def send_message(agent_id, message, chat_id=0, keyboard=None):
        delivered.append(message)
        return {"ok": True, "message_id": 1}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"tq-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunFlow],
            activities=[launch, check, send_message, _cleanup_activity(cleanup_calls)],
        ):
            result = await env.client.execute_workflow(
                AgentRunFlow.run,
                AgentRunInput(
                    agent_id="sebas",
                    prompt="stalling job",
                    purpose="stalling job",
                    timeout_minutes=1,
                ),
                id=f"arf-stalled-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result["status"] == "timeout"
    assert 60 <= result["elapsed_s"] <= 66, result["elapsed_s"]
    assert len(delivered) == 1
    assert "still running" in delivered[0]


@pytest.mark.asyncio
async def test_elapsed_s_is_wall_clock_including_the_time_spent_in_the_last_poll():
    """`elapsed_s` must be launch → terminal state, not launch → last sample.

    Prod reported `elapsed_s: 62` for a run that took 2h17m — and its ungated
    twin reported exactly 62 too, which is the tell: 62 is two poll intervals
    plus launch latency, i.e. the sample taken before the poll that eventually
    answered. Here the poll that reports `finished` takes 3 seconds, so the
    stale sample (60) and the real wall clock (63+) are distinguishable.

    Falsifiable: move the `elapsed` computation back above the poll and this
    fails at 60.
    """
    calls = {"count": 0}
    cleanup_calls: list[dict] = []

    @activity.defn(name="launch_agent_run")
    async def launch(
        prompt, repo="", engine="", purpose="", agent_id="", gated=False, timeout_minutes=0
    ):
        return _launched()

    @activity.defn(name="check_agent_run")
    async def check(output_file, host="", probe_alive=True):
        calls["count"] += 1
        if calls["count"] < 2:
            return {"status": "running", "output": "", "reason": ""}
        await asyncio.sleep(3)
        return {"status": "finished", "output": "done at last", "reason": ""}

    @activity.defn(name="send_message")
    async def send_message(agent_id, message, chat_id=0, keyboard=None):
        return {"ok": True, "message_id": 1}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"tq-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunFlow],
            activities=[launch, check, send_message, _cleanup_activity(cleanup_calls)],
        ):
            result = await env.client.execute_workflow(
                AgentRunFlow.run,
                AgentRunInput(agent_id="sebas", prompt="slow finish", purpose="slow finish"),
                id=f"arf-elapsed-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result["status"] == "ok"
    # Two 30s poll intervals (skipped) + the 3s the final poll really took.
    assert result["elapsed_s"] >= 62, result["elapsed_s"]
    assert result["elapsed_s"] <= 120, result["elapsed_s"]


class _FakeRemoteScript:
    """Minimal stand-in for RemoteScriptConnector's polling surface. Signatures
    are copied from the real class so a rename there breaks these tests."""

    def __init__(self, outputs: list[str | None], alive: bool):
        self.outputs = outputs
        self.alive = alive
        self.alive_calls = 0
        self.removed: list[str] = []

    async def fetch_kimi_run_output(self, output_file: str, host: str = "") -> str | None:
        return self.outputs.pop(0) if self.outputs else None

    async def kimi_run_alive(self, output_file: str, host: str = "") -> bool:
        self.alive_calls += 1
        return self.alive

    async def remove_worktree(self, worktree_path: str, host: str = "") -> None:
        self.removed.append(worktree_path)


@pytest.mark.asyncio
async def test_check_finished_when_process_exited_with_output():
    """A general run has no STATUS footer, so process exit + a transcript is
    what 'finished' means."""
    raw = '{"role":"assistant","content":[{"type":"text","text":"All done. 3 issues found."}]}'
    acts = AgentRunActivities(remote_script=_FakeRemoteScript([raw, raw], alive=False))
    out = await acts.check_agent_run("/tmp/run.jsonl", "node-a", True)
    assert out["status"] == "finished"
    assert "3 issues found" in out["output"]


@pytest.mark.asyncio
async def test_check_failed_when_process_exited_with_nothing():
    """Early death (bad CLI flag) — no transcript to deliver, so say so rather
    than reporting a successful run with an empty body."""
    acts = AgentRunActivities(remote_script=_FakeRemoteScript([None, None], alive=False))
    out = await acts.check_agent_run("/tmp/run.jsonl", "node-a", True)
    assert out["status"] == "failed"
    assert "exited" in out["reason"]


@pytest.mark.asyncio
async def test_check_still_running_while_process_holds_the_file():
    acts = AgentRunActivities(remote_script=_FakeRemoteScript(["partial"], alive=True))
    out = await acts.check_agent_run("/tmp/run.jsonl", "node-a", True)
    assert out["status"] == "running"


@pytest.mark.asyncio
async def test_check_does_not_probe_liveness_during_the_launch_grace():
    """probe_alive=False must not even ask — a dead read here would end a
    healthy run that hasn't opened its output file yet."""
    fake = _FakeRemoteScript([None], alive=False)
    acts = AgentRunActivities(remote_script=fake)
    out = await acts.check_agent_run("/tmp/run.jsonl", "node-a", False)
    assert out["status"] == "running"
    assert fake.alive_calls == 0


@pytest.mark.asyncio
async def test_cleanup_removes_the_worktree_of_a_dead_run():
    """The whole point of #300: five prod runs left five worktrees and five
    `git worktree list` registrations behind because nothing ever called this."""
    fake = _FakeRemoteScript([], alive=False)
    acts = AgentRunActivities(remote_script=fake)
    out = await acts.cleanup_agent_run("/w/aegis-scratch-aegis-wt/ab12", "/tmp/run.jsonl", "node-a")
    assert out["removed"] is True
    assert fake.removed == ["/w/aegis-scratch-aegis-wt/ab12"]


@pytest.mark.asyncio
async def test_cleanup_leaves_a_live_runs_worktree_alone():
    """Deleting the cwd of a running agent is worse than leaking a directory.
    The probe also fails OPEN — an inconclusive `fuser` reads as alive — so an
    unreachable host leaks rather than destroys."""
    fake = _FakeRemoteScript([], alive=True)
    acts = AgentRunActivities(remote_script=fake)
    out = await acts.cleanup_agent_run("/w/aegis-scratch-aegis-wt/ab12", "/tmp/run.jsonl", "node-a")
    assert out["removed"] is False
    assert "alive" in out["reason"]
    assert fake.removed == []


@pytest.mark.asyncio
async def test_cleanup_without_an_output_file_skips_the_probe():
    """Falsifiability pair with the test above: same `alive=True` connector,
    opposite outcome, and the only difference is the empty `output_file` the
    flow passes once a poll has already observed the process exit."""
    fake = _FakeRemoteScript([], alive=True)
    acts = AgentRunActivities(remote_script=fake)
    out = await acts.cleanup_agent_run("/w/aegis-scratch-aegis-wt/ab12", "", "node-a")
    assert out["removed"] is True
    assert fake.alive_calls == 0
    assert fake.removed == ["/w/aegis-scratch-aegis-wt/ab12"]


@pytest.mark.asyncio
async def test_cleanup_with_no_worktree_does_nothing():
    """`start_kimi_run` falls back to the SHARED clone when `worktree add`
    fails, and reports an empty path for it. Removing that would delete the
    checkout every future run depends on."""
    fake = _FakeRemoteScript([], alive=False)
    acts = AgentRunActivities(remote_script=fake)
    out = await acts.cleanup_agent_run("", "/tmp/run.jsonl", "node-a")
    assert out["removed"] is False
    assert fake.removed == []
    assert fake.alive_calls == 0


class _LaunchRecorder:
    """Records the launch call. `start_kimi_run`'s signature is copied from the
    real connector so a rename or reorder there breaks this test."""

    def __init__(self):
        self.calls: list[dict] = []

    async def coding_settings(self) -> dict:
        return {"kimi_binary": "/bin/kimi", "repo_base": "/w"}

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
        self.calls.append(
            {
                "repo": repo,
                "engine_override": engine_override,
                "agent_id": agent_id,
                "gated": gated,
                "token_ttl_seconds": token_ttl_seconds,
            }
        )
        return {"status": "running", "run_id": "r1", "output_file": "/tmp/o", "engine": "claude"}


@pytest.mark.asyncio
async def test_launch_forwards_agent_id_to_the_connector():
    """The dispatching agent decides WHICH AEGIS tool surface the run mounts
    (`/api/mcp-server/{agent_id}`). If the activity drops it, the run launches
    perfectly and silently has no AEGIS tools at all — nothing else fails, so
    this assertion is the only thing standing between that and production."""
    rec = _LaunchRecorder()
    acts = AgentRunActivities(remote_script=rec)
    out = await acts.launch_agent_run("do a thing", "", "", "audit", "pandoras-actor")
    assert out["status"] == "running"
    assert rec.calls == [
        {
            "repo": "aegis-scratch",
            "engine_override": "",
            "agent_id": "pandoras-actor",
            "gated": False,
            # No timeout supplied ⇒ 0 ⇒ the connector's default mount-token TTL.
            "token_ttl_seconds": 0,
        }
    ]


@pytest.mark.asyncio
async def test_launch_without_agent_id_passes_empty_not_a_placeholder():
    """No agent ⇒ no mount. The connector's own gate keys on an EMPTY agent_id,
    so inventing a default here would point a run at a nonexistent endpoint."""
    rec = _LaunchRecorder()
    acts = AgentRunActivities(remote_script=rec)
    await acts.launch_agent_run("do a thing")
    assert rec.calls[0]["agent_id"] == ""


@pytest.mark.asyncio
async def test_launch_forwards_gated_to_the_connector():
    """A dropped `gated` is the quietest failure in this lane: the run launches
    fine, reports success, and mutates whatever it likes with nobody asked. The
    connector is the only enforcement point, so it has to receive the flag."""
    rec = _LaunchRecorder()
    acts = AgentRunActivities(remote_script=rec)
    out = await acts.launch_agent_run("do a thing", "bcp", "claude", "audit", "sebas", True)
    assert out["status"] == "running"
    assert rec.calls[0]["gated"] is True


@pytest.mark.asyncio
async def test_launch_defaults_to_ungated():
    """Falsifiability control: gated must be opt-in, never the default."""
    rec = _LaunchRecorder()
    acts = AgentRunActivities(remote_script=rec)
    await acts.launch_agent_run("do a thing")
    assert rec.calls[0]["gated"] is False


def test_the_fakes_signatures_really_do_match_the_real_connector():
    """The fakes above CLAIM to mirror `RemoteScriptConnector`, and until this
    test nothing checked it — a renamed or reordered parameter on the real
    class left every test in this file passing against a fake that no longer
    resembles it, which is the whole failure mode fakes are supposed to avoid.

    `inspect.signature(...).bind(...)` is the check: it accepts only calls the
    REAL method would accept, with the same names, and the fakes are exercised
    with the same keyword sets the activity uses.
    """
    import inspect

    from aegis.connectors.remote_script import RemoteScriptConnector

    real_launch = inspect.signature(RemoteScriptConnector.start_kimi_run)
    fake_launch = inspect.signature(_LaunchRecorder.start_kimi_run)
    launch_kwargs = {
        "repo": "aegis-scratch",
        "prompt": "p",
        "kimi_binary": "/bin/kimi",
        "engine_override": "claude",
        "agent_id": "sebas",
        "gated": True,
    }
    real_launch.bind(None, **launch_kwargs)
    fake_launch.bind(None, **launch_kwargs)
    # Same parameter NAMES in the same order — the activity passes keywords, so
    # a reorder is survivable, but a rename silently drops to a default.
    assert list(fake_launch.parameters) == list(real_launch.parameters)

    for name in ("fetch_kimi_run_output", "kimi_run_alive"):
        real = inspect.signature(getattr(RemoteScriptConnector, name))
        fake = inspect.signature(getattr(_FakeRemoteScript, name))
        assert list(fake.parameters) == list(real.parameters), name
        real.bind(None, output_file="/tmp/run.jsonl", host="node-a")
        fake.bind(None, output_file="/tmp/run.jsonl", host="node-a")

    real_rm = inspect.signature(RemoteScriptConnector.remove_worktree)
    fake_rm = inspect.signature(_FakeRemoteScript.remove_worktree)
    assert list(fake_rm.parameters) == list(real_rm.parameters)
    real_rm.bind(None, worktree_path="/w/r-aegis-wt/ab12", host="node-a")
    fake_rm.bind(None, worktree_path="/w/r-aegis-wt/ab12", host="node-a")


@pytest.mark.asyncio
async def test_gated_input_reaches_the_launch_activity():
    """`AgentRunInput.gated` → `launch_agent_run(..., gated)`. The flow is the
    only carrier between the dispatcher's request and the connector that
    enforces it; a dropped flag here produces an ungated run that reports
    success — exactly the outcome the gate exists to prevent."""
    launch_calls: list[dict] = []
    cleanup_calls: list[dict] = []

    @activity.defn(name="launch_agent_run")
    async def launch(
        prompt, repo="", engine="", purpose="", agent_id="", gated=False, timeout_minutes=0
    ):
        launch_calls.append({"gated": gated, "engine": engine})
        return _launched()

    @activity.defn(name="check_agent_run")
    async def check(output_file, host="", probe_alive=True):
        return {"status": "finished", "output": "done", "reason": ""}

    @activity.defn(name="send_message")
    async def send_message(agent_id, message, chat_id=0, keyboard=None):
        return {"ok": True, "message_id": 1}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"tq-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunFlow],
            activities=[launch, check, send_message, _cleanup_activity(cleanup_calls)],
        ):
            for gated in (True, False):
                await env.client.execute_workflow(
                    AgentRunFlow.run,
                    AgentRunInput(
                        agent_id="sebas",
                        prompt="Refactor the retry policy.",
                        engine="claude",
                        gated=gated,
                    ),
                    id=f"arf-gated-{gated}-{uuid.uuid4().hex[:8]}",
                    task_queue=task_queue,
                )

    # Both values cross the boundary — a hardcoded True or False fails.
    assert [call["gated"] for call in launch_calls] == [True, False]
