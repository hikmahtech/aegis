"""AgentRunFlow — the heavy lane: any agent dispatches a headless CLI run.

AEGIS has two lanes for agent work. The LIGHT lane is core's in-process chat
loop (`services/chat.py`): one LLM call plus a bounded tool loop, answering in
the same turn. The HEAVY lane is this flow: a full claude/kimi CLI session on
the coding host — its own filesystem, its own tool budget, minutes to tens of
minutes — whose result is delivered back to the agent's channel when it lands.

This is the coding-run machinery (`activities/agent_run.py` →
`RemoteScriptConnector.start_kimi_run`) with the Todoist task coupling
removed, so the work is not required to be "a code change ending in a PR":
investigation, research and analysis are equally valid asks. A run with no
repo of its own gets the `aegis-scratch` checkout.

Shape:

  1. launch  — NO_RETRY (see below), failure is delivered and ends the flow
  2. poll    — `workflow.sleep(30s)` → a read-only check activity, until the
               run reports finished/failed or `timeout_minutes` elapses
  3. deliver — one chat message: header + the tail of the transcript

The launch is NO_RETRY because it is NOT idempotent: a retry is a SECOND CLI
session on a second worktree, burning tokens and racing the first one's
writes. Same invariant as `synthesize_reply` in AgentChatReplyFlow (PR #283).
Polling, by contrast, is a `cat` and a `fuser` — retried freely on STANDARD.

A timeout does NOT kill the run. The process may still be minutes from a good
answer, and the operator can attach to its tmux window; killing it would throw
away the work to report a deadline. The flow says where to look and exits.

**Both clocks come from `workflow.now()` and are sampled where they are USED**
(issue #295). The loop used to read the clock once at the top of an iteration
and then reuse that number after the poll activity — which made the deadline
unenforceable for exactly the case it exists for. A run whose engine stalled
for 2h17m inside one poll returned status `ok` with `elapsed_s: 62`: 62 was the
sample taken before the activity that eventually answered, and no deadline
check ran while the flow was blocked in it. So the poll is now bounded by
`schedule_to_close_timeout` (the only timeout that covers queue wait AND every
retry, not just one attempt) and every deadline/elapsed decision re-reads the
clock afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.agent_run import SCRATCH_REPO, AgentRunActivities
    from aegis_worker.activities.delivery import DeliveryActivities
    from aegis_worker.shared.retry import (
        NO_RETRY,
        STANDARD,
        TIMEOUT_FAST,
        TIMEOUT_LONG,
        TIMEOUT_STANDARD,
    )

_POLL_INTERVAL = timedelta(seconds=30)
# Tail of the transcript delivered to chat. Slack truncates long messages and
# the run's conclusion is at the end, so we keep the end, not the start.
_OUTPUT_TAIL = 3000


def _err_str(exc: BaseException) -> str:
    """Unwrap ActivityError cause chain to get the original message."""
    cause = exc.__cause__ or exc
    return str(cause)[:200]


def _elapsed_str(seconds: int) -> str:
    return f"{seconds // 60}m{seconds % 60:02d}s"


@dataclass
class AgentRunInput:
    """`agent_id` is first per the flow-config convention — the run recorder
    interceptor reads it to populate `workflow_runs.agent_id`, it is the agent
    whose channel the result is delivered to, and it is the agent whose AEGIS
    tool surface a claude run mounts over MCP.

    `repo` None ⇒ the `aegis-scratch` checkout. `engine` "" ⇒ the connector's
    own org routing decides claude vs kimi.

    `gated` requires a human to approve every action the CLI would otherwise
    auto-allow. It is claude-only and needs the MCP mount; the connector refuses
    the launch rather than downgrading to an ungated run, so a gated request
    that cannot be honoured surfaces as a delivered failure.
    """

    agent_id: str
    prompt: str
    repo: str | None = None
    engine: str = ""
    purpose: str = ""
    timeout_minutes: int = 30
    gated: bool = False


@workflow.defn(name="AgentRunFlow")
class AgentRunFlow:
    @workflow.run
    async def run(self, inp: AgentRunInput) -> dict:
        purpose = (inp.purpose or "").strip() or "agent run"
        repo = (inp.repo or "").strip() or SCRATCH_REPO
        workflow.logger.info(
            "agent_run_flow_starting agent=%s repo=%s engine=%s purpose=%s gated=%s",
            inp.agent_id,
            repo,
            inp.engine or "auto",
            purpose,
            inp.gated,
        )

        # Step 1 — launch. NO_RETRY: a second attempt is a second CLI session.
        try:
            launched = await workflow.execute_activity_method(
                AgentRunActivities.launch_agent_run,
                args=[inp.prompt, repo, inp.engine, purpose, inp.agent_id, inp.gated],
                start_to_close_timeout=TIMEOUT_LONG,
                retry_policy=NO_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 — degrade visibly, never retry
            launched = {"status": "failed", "error": _err_str(exc)}

        if launched.get("status") != "running":
            reason = str(launched.get("error") or "launch failed")
            await self._deliver(
                inp.agent_id,
                f"Couldn't start the {purpose} run on `{repo}`: {reason}"
                + _provision_hint(reason, repo, str(launched.get("repo_base") or "")),
            )
            workflow.logger.warning("agent_run_launch_failed repo=%s reason=%s", repo, reason)
            return _result("failed", reason, launched.get("run_id", ""), "", "", 0)

        run_id = str(launched.get("run_id") or "")
        engine = str(launched.get("engine") or "") or (inp.engine or "auto")
        host = str(launched.get("host") or "")
        output_file = str(launched.get("output_file") or "")
        tmux_window = str(launched.get("tmux_window") or "")
        deadline_s = max(1, int(inp.timeout_minutes or 30)) * 60
        # The one clock everything below is measured against: the moment the
        # run actually started. Sampled here, never re-based.
        launched_at = workflow.now()

        # Step 2 — poll. The FIRST check skips the liveness probe: SSH and
        # checkout latency mean the agent binary may not hold the output file
        # yet, and a "dead" read there would end a healthy run at 30 seconds.
        probe_alive = False
        while True:
            elapsed = int((workflow.now() - launched_at).total_seconds())
            if elapsed >= deadline_s:
                # Step 4 — deadline reached. Deliberately NOT killed. This is
                # the ONLY timeout site: every path that could burn time (the
                # sleep, a slow or stuck poll) is bounded so control returns
                # here rather than sailing past the deadline (#295).
                where = (
                    f"tmux window `{tmux_window}` on {host}"
                    if tmux_window
                    else f"launched detached (no tmux window) on {host or '?'}"
                )
                await self._deliver(
                    inp.agent_id,
                    f"{purpose} — still running after {inp.timeout_minutes} min "
                    f"(engine={engine} run={run_id}). It has NOT been stopped.\n"
                    f"attach: {where}; output: {output_file}",
                )
                workflow.logger.warning("agent_run_timeout run_id=%s elapsed_s=%d", run_id, elapsed)
                return _result(
                    "timeout",
                    f"still running after {inp.timeout_minutes} min",
                    run_id,
                    engine,
                    host,
                    elapsed,
                )

            # Never sleep past the deadline: the wait is the poll interval or
            # what is left of the budget, whichever is shorter.
            await workflow.sleep(min(_POLL_INTERVAL, timedelta(seconds=deadline_s - elapsed)))
            remaining_s = deadline_s - int((workflow.now() - launched_at).total_seconds())
            if remaining_s <= 0:
                # The deadline arrived while sleeping — report it at the top of
                # the loop rather than spending the budget on one more poll.
                continue
            try:
                check = await workflow.execute_activity_method(
                    AgentRunActivities.check_agent_run,
                    args=[output_file, host, probe_alive],
                    start_to_close_timeout=TIMEOUT_STANDARD,
                    # The load-bearing one. `start_to_close` bounds a single
                    # ATTEMPT once a worker has picked the task up; it says
                    # nothing about queue wait or about the retry sequence, so
                    # a poll that cannot complete could hold this loop — and
                    # therefore the deadline check — for hours. Capping the
                    # whole call at what is left of the budget means no poll
                    # can outlive the deadline it is supposed to be checked
                    # against.
                    schedule_to_close_timeout=timedelta(seconds=remaining_s),
                    retry_policy=STANDARD,
                )
            except Exception as exc:  # noqa: BLE001
                # A poll that fails past its retries says nothing about the
                # run itself (flaky SSH). Treat it as "still running" — the
                # deadline at the top of the loop is what bounds this.
                workflow.logger.warning(
                    "agent_run_check_failed run_id=%s err=%s", run_id, _err_str(exc)
                )
                check = {"status": "running", "output": "", "reason": ""}
            probe_alive = True

            status = str(check.get("status") or "running")
            if status in ("finished", "failed"):
                # Re-read the clock: `elapsed` above was sampled BEFORE the
                # poll, and the poll is exactly where a stalled run spends its
                # time. Reporting the stale sample is what produced
                # `elapsed_s: 62` for a 2h17m run.
                elapsed = int((workflow.now() - launched_at).total_seconds())
                output = str(check.get("output") or "")[-_OUTPUT_TAIL:]
                header = (
                    f"{purpose} — {'done' if status == 'finished' else 'failed'} "
                    f"in {_elapsed_str(elapsed)}\n"
                    f"engine={engine} host={host or '?'} run={run_id} repo={repo}"
                )
                body = output or str(check.get("reason") or "no output")
                await self._deliver(inp.agent_id, f"{header}\n\n{body}")
                workflow.logger.info(
                    "agent_run_flow_done run_id=%s status=%s elapsed_s=%d",
                    run_id,
                    status,
                    elapsed,
                )
                return _result(
                    "ok" if status == "finished" else "failed",
                    str(check.get("reason") or "") or None,
                    run_id,
                    engine,
                    host,
                    elapsed,
                )

    async def _deliver(self, agent_id: str, text: str) -> None:
        """Send to the agent's bound channel. A delivery failure must not fail
        the flow — the run already happened and its outcome is in the result."""
        try:
            await workflow.execute_activity_method(
                DeliveryActivities.send_message,
                args=[agent_id, text],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=STANDARD,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning(
                "agent_run_delivery_failed agent=%s err=%s", agent_id, _err_str(exc)
            )


def _result(
    status: str, reason: str | None, run_id: str, engine: str, host: str, elapsed_s: int
) -> dict:
    """Uniform result shape — every return path carries all six keys so
    `workflow_runs.result_summary` is queryable without coalescing."""
    return {
        "status": status,
        "reason": reason,
        "run_id": run_id,
        "engine": engine,
        "host": host,
        "elapsed_s": elapsed_s,
    }


def _provision_hint(reason: str, repo: str, repo_base: str) -> str:
    """Turn the connector's "checkout missing" hard failure into the one-liner
    that fixes it. `start_kimi_run` never JIT-clones, so this is the ONLY
    remedy — and for `aegis-scratch` there is no upstream to clone from, which
    is why the empty commit is part of it (a detached worktree needs a HEAD)."""
    if "checkout missing" not in reason.lower():
        return ""
    base = repo_base or "<repo_base>"
    return (
        f"\n\nProvision it once on the coding host:\n"
        f"  mkdir -p {base}/{repo} && cd {base}/{repo} && "
        f"git init && git commit --allow-empty -m init"
    )
