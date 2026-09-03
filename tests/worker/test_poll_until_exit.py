"""`poll_until_exit` — the poll loop both agent lanes share.

The loop used to live inside `AgentRunFlow.run`, where the only way to test it
was through that flow's deliver/cleanup handling — and the flow's result shape
has no room for `final`, so the one value the coding lane actually posts back
to Todoist was untestable. These tests drive the helper directly through a
throwaway workflow, so they pin the two things a second caller depends on: the
deadline really fires at the deadline, and the run's own final message survives
the trip out of the activity.
"""

from __future__ import annotations

import uuid

import pytest
from aegis_worker.flows.agent_run import poll_until_exit
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


@workflow.defn(name="PollUntilExitProbe")
class _PollProbe:
    """Nothing but a `@workflow.run` to call the helper from — it needs a
    workflow context for `workflow.now()`, `workflow.sleep` and the activity."""

    @workflow.run
    async def run(self, deadline_s: int) -> dict:
        return await poll_until_exit(
            output_file="/tmp/aegis-kimi-run-ab12cd34.jsonl",
            host="node-a",
            deadline_s=deadline_s,
            launched_at=workflow.now(),
        )


async def _run_probe(check, deadline_s: int) -> dict:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"tq-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[_PollProbe],
            activities=[check],
        ):
            return await env.client.execute_workflow(
                _PollProbe.run,
                deadline_s,
                id=f"poll-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )


@pytest.mark.asyncio
async def test_timeout_fires_at_the_deadline_with_no_output():
    """A run that never exits ends as `timeout` AT the deadline, carrying the
    elapsed time and nothing else — the caller decides what a timeout means.

    The upper bound is the assertion that matters: "fired eventually" is what
    #295 shipped to prod. The first poll must also skip the liveness probe, or
    launch latency reads as a dead run 30 seconds in."""
    calls: list[dict] = []

    @activity.defn(name="check_agent_run")
    async def check(output_file, host="", probe_alive=True):
        calls.append({"output_file": output_file, "host": host, "probe_alive": probe_alive})
        return {"status": "running", "output": "", "reason": "", "final": ""}

    out = await _run_probe(check, 60)

    assert out["status"] == "timeout"
    assert 60 <= out["elapsed_s"] <= 90, out["elapsed_s"]
    assert out["output"] == ""
    assert out["final"] == ""
    assert set(out) == {"status", "output", "final", "reason", "elapsed_s"}
    # Polled the whole way, and the first look did not probe liveness.
    assert len(calls) >= 1
    assert calls[0]["probe_alive"] is False
    assert calls[0]["output_file"] == "/tmp/aegis-kimi-run-ab12cd34.jsonl"
    assert calls[0]["host"] == "node-a"
    assert all(call["probe_alive"] for call in calls[1:])


@pytest.mark.asyncio
async def test_finished_returns_the_runs_final_message():
    """`final` is the CLI's own last message and the coding lane posts it as a
    comment. It has to reach the caller verbatim, alongside the transcript —
    returning one or the other is what makes a lane pick the wrong text."""

    @activity.defn(name="check_agent_run")
    async def check(output_file, host="", probe_alive=True):
        return {
            "status": "finished",
            "output": "Read 14 files.\nOpened PR #12.",
            "reason": "",
            "final": "Opened PR #12.\n\nSTATUS: implemented",
        }

    out = await _run_probe(check, 600)

    assert out["status"] == "finished"
    assert out["final"] == "Opened PR #12.\n\nSTATUS: implemented"
    assert out["output"] == "Read 14 files.\nOpened PR #12."
    assert out["reason"] == ""
    assert out["elapsed_s"] > 0
