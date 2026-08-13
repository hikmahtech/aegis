"""AgentRunFlow — happy path, non-retried launch failure, and timeout.

The flow is the heavy lane: launch a headless CLI run, poll it, deliver the
transcript tail to the agent's channel. The three cases below lock the three
behaviours that are expensive to get wrong — a delivered result, a launch that
is attempted exactly ONCE (a retry is a second billed CLI session), and a
deadline that reports where the still-running process is instead of killing it.
"""

from __future__ import annotations

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
        "repo": "scratch",
        "run_id": "ab12cd34",
        "output_file": "/tmp/aegis-kimi-run-ab12cd34.jsonl",
        "host": "node-a",
        "engine": "claude",
        "in_tmux": True,
        "tmux_window": "claude-scratch-ab12cd34",
        "worktree_path": "/w/scratch-aegis-wt/ab12cd34",
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_happy_path_delivers_output_tail():
    """Launch ok → first check finished → transcript tail delivered, status ok."""
    launch_calls: list[dict] = []
    check_calls: list[dict] = []
    delivered: list[str] = []
    transcript = "Read 14 files.\n\nSUMMARY: the retry policy is unbounded."

    @activity.defn(name="launch_agent_run")
    async def launch(prompt, repo="", engine="", purpose="", agent_id="", gated=False):
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
            activities=[launch, check, send_message],
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
            "repo": "scratch",
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
    checked = {"hit": False}

    @activity.defn(name="launch_agent_run")
    async def launch(prompt, repo="", engine="", purpose="", agent_id="", gated=False):
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
            activities=[launch, check, send_message],
        ):
            result = await env.client.execute_workflow(
                AgentRunFlow.run,
                AgentRunInput(agent_id="sebas", prompt="do a thing", purpose="thing"),
                id=f"arf-launchfail-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert attempts["count"] == 1, "launch must be attempted exactly once (NO_RETRY)"
    assert checked["hit"] is False
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

    @activity.defn(name="launch_agent_run")
    async def launch(prompt, repo="", engine="", purpose="", agent_id="", gated=False):
        return {
            "status": "failed",
            "error": "Repo checkout missing on node-a: /w/scratch — provision it",
            "repo": "scratch",
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
            activities=[launch, check, send_message],
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
    assert "/w/scratch" in delivered[0]


@pytest.mark.asyncio
async def test_timeout_reports_where_the_run_is_and_does_not_kill_it():
    """Deadline reached with the run still alive: report the tmux window and
    the output file, return status timeout, and DON'T stop the process."""
    delivered: list[str] = []
    checks = {"count": 0}

    @activity.defn(name="launch_agent_run")
    async def launch(prompt, repo="", engine="", purpose="", agent_id="", gated=False):
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
            activities=[launch, check, send_message],
        ):
            result = await env.client.execute_workflow(
                AgentRunFlow.run,
                AgentRunInput(
                    agent_id="sebas",
                    prompt="long job",
                    purpose="long job",
                    timeout_minutes=1,
                ),
                id=f"arf-timeout-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result["status"] == "timeout"
    assert result["run_id"] == "ab12cd34"
    assert result["elapsed_s"] >= 60
    assert set(result) == {"status", "reason", "run_id", "engine", "host", "elapsed_s"}
    # The run kept being polled until the deadline (not abandoned after one look).
    assert checks["count"] >= 2
    assert len(delivered) == 1
    msg = delivered[0]
    assert "still running" in msg
    assert "kimi-scratch-ab12cd34" in msg
    assert "/tmp/aegis-kimi-run-ab12cd34.jsonl" in msg
    assert "NOT been stopped" in msg


class _FakeRemoteScript:
    """Minimal stand-in for RemoteScriptConnector's polling surface. Signatures
    are copied from the real class so a rename there breaks these tests."""

    def __init__(self, outputs: list[str | None], alive: bool):
        self.outputs = outputs
        self.alive = alive
        self.alive_calls = 0

    async def fetch_kimi_run_output(self, output_file: str, host: str = "") -> str | None:
        return self.outputs.pop(0) if self.outputs else None

    async def kimi_run_alive(self, output_file: str, host: str = "") -> bool:
        self.alive_calls += 1
        return self.alive


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
    ) -> dict:
        self.calls.append(
            {
                "repo": repo,
                "engine_override": engine_override,
                "agent_id": agent_id,
                "gated": gated,
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
            "repo": "scratch",
            "engine_override": "",
            "agent_id": "pandoras-actor",
            "gated": False,
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


@pytest.mark.asyncio
async def test_gated_input_reaches_the_launch_activity():
    """`AgentRunInput.gated` → `launch_agent_run(..., gated)`. The flow is the
    only carrier between the dispatcher's request and the connector that
    enforces it; a dropped flag here produces an ungated run that reports
    success — exactly the outcome the gate exists to prevent."""
    launch_calls: list[dict] = []

    @activity.defn(name="launch_agent_run")
    async def launch(prompt, repo="", engine="", purpose="", agent_id="", gated=False):
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
            activities=[launch, check, send_message],
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
