"""AgentRunActivities — dispatch an arbitrary headless coding-CLI run.

Same machinery as the coding lane in ``activities/agent_task.py``
(``RemoteScriptConnector.start_kimi_run`` on the coding host, a per-run git
worktree, a tmux window when the host is reachable), reached the same way —
the connector directly off ``self.remote_script``, not over core's HTTP API.
What is dropped is the Todoist coupling: ``run_task_investigation`` /
``run_task_implementation`` hard-code their prompts and take a ``task_id`` +
``github_repo``, so neither could carry a free-form ask.

Completion is detected by PROCESS EXIT, not by the ``STATUS:`` footer that
``alerts._kimi_output_complete`` looks for. That regex accepts a closed
vocabulary of alert-RCA and Jira-scoping verbs (``investigated``,
``scoped``, …) which a general-purpose run has no reason to emit, so keying
on it would make every run wait out its full timeout. ``kimi_run_alive``
(``fuser`` on the output file, fail-OPEN on a flaky probe) is the same
liveness signal ``AlertActivities`` already uses to fail an early-death run
fast, and for a general run it is the whole completion signal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from temporalio import activity

# A run with no repo of its own gets this fixed checkout. `start_kimi_run`
# never JIT-clones — a missing directory is a deliberate hard failure — so the
# operator provisions it once (see docs/development.md):
#   mkdir -p <repo_base>/scratch && cd <repo_base>/scratch &&
#   git init && git commit --allow-empty -m init
# The empty commit matters: the launch adds a detached worktree, which needs a
# HEAD to detach from.
SCRATCH_REPO = "scratch"

# The CLI writes stream-json, so escapes are rare — but a plain-text banner
# (or a non-stream-json fallback mode) tees straight through, and the tail we
# paste into chat has to stay readable.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    """Drop ANSI escape sequences from run output."""
    return _ANSI_RE.sub("", text)


@dataclass
class AgentRunActivities:
    """Launch + poll a headless agent run. `remote_script` is the shared
    RemoteScriptConnector instance (None ⇒ every activity reports failed
    rather than raising, per the connector-optional convention)."""

    remote_script: Any = None

    @activity.defn
    async def launch_agent_run(
        self,
        prompt: str,
        repo: str = "",
        engine: str = "",
        purpose: str = "",
        agent_id: str = "",
        gated: bool = False,
    ) -> dict:
        """Start one CLI run on the coding host. NOT idempotent — see the flow.

        `repo` is a workspace-relative FIXED checkout under `repo_base`;
        empty ⇒ SCRATCH_REPO. `engine` ("claude"|"kimi") forces the engine,
        empty leaves the connector's org routing in charge.

        `agent_id` is the dispatching agent, and it is load-bearing rather than
        decorative: the connector mounts THAT agent's AEGIS tool surface into a
        claude run over MCP (`/api/mcp-server/{agent_id}`). Dropping it here
        silently produces a run with no AEGIS tools.

        `gated` asks for human approval on every non-auto-allowed action. Same
        class of bug as dropping `agent_id`, only worse: a dropped `gated` gives
        a perfectly healthy run that quietly mutates things nobody approved.
        The connector enforces the preconditions and returns `failed` when it
        cannot honour the request — this activity only has to forward it.
        """
        if self.remote_script is None:
            return {
                "status": "failed",
                "error": "remote_script connector is not configured",
                "repo": repo or SCRATCH_REPO,
            }
        target_repo = (repo or "").strip() or SCRATCH_REPO
        settings = await self.remote_script.coding_settings()
        started = await self.remote_script.start_kimi_run(
            repo=target_repo,
            prompt=_compose_prompt(prompt, purpose),
            kimi_binary=settings.get("kimi_binary", ""),
            engine_override=(engine or "").strip(),
            agent_id=(agent_id or "").strip(),
            gated=bool(gated),
        )
        if started.get("status") != "running":
            return {
                "status": "failed",
                "error": str(started.get("error") or "")[:500],
                "repo": target_repo,
                # The flow turns repo_base into a copy-pasteable provision
                # command when the checkout is what was missing.
                "repo_base": settings.get("repo_base", ""),
                "run_id": started.get("run_id", ""),
                "engine": started.get("engine", ""),
            }
        run_id = started.get("run_id", "")
        engine_used = started.get("engine", "")
        return {
            "status": "running",
            "repo": target_repo,
            "run_id": run_id,
            "output_file": started.get("output_file", ""),
            "host": started.get("host", ""),
            "engine": engine_used,
            "in_tmux": bool(started.get("in_tmux")),
            # Same composition start_kimi_run's `_launch_in_tmux` uses, so the
            # name we hand the operator is the name they can attach to.
            "tmux_window": (
                _tmux_window_name(engine_used, target_repo, run_id)
                if started.get("in_tmux")
                else ""
            ),
            "worktree_path": started.get("worktree_path", ""),
        }

    @activity.defn
    async def check_agent_run(
        self, output_file: str, host: str = "", probe_alive: bool = True
    ) -> dict:
        """One read-only poll: fetch the output, then probe liveness.

        Read-only and idempotent by construction (a `cat` and a `fuser`), so
        the caller can retry it freely — unlike `launch_agent_run`.

        `probe_alive=False` skips the liveness probe, which the flow passes on
        its first poll: same launch grace period `AlertActivities` gives its
        own loop, so SSH/checkout latency can never read as a dead run.

        Returns status `running` | `finished` | `failed`. Process exit with no
        recoverable transcript is `failed` (early death — e.g. a bad CLI flag);
        process exit WITH a transcript is `finished`, because a general run has
        no success footer to check and partial output is still the answer.
        """
        from aegis_worker.activities.alerts import (
            _INVESTIGATION_OUTPUT_CAP,
            _extract_kimi_transcript,
        )

        if self.remote_script is None or not output_file:
            return {"status": "failed", "output": "", "reason": "no run to poll"}

        raw = await self.remote_script.fetch_kimi_run_output(output_file, host=host) or ""
        if probe_alive and not await self.remote_script.kimi_run_alive(output_file, host=host):
            # Process is gone. Re-fetch once — it may have written its final
            # events between our fetch and the probe.
            raw = await self.remote_script.fetch_kimi_run_output(output_file, host=host) or raw
            transcript = strip_ansi(_extract_kimi_transcript(raw))[-_INVESTIGATION_OUTPUT_CAP:]
            if not transcript.strip():
                activity.logger.warning(
                    "agent_run_early_death output_file=%s host=%s", output_file, host
                )
                return {
                    "status": "failed",
                    "output": "",
                    "reason": (
                        "the agent process exited without producing any output "
                        "(early death — e.g. a CLI flag error)"
                    ),
                }
            return {"status": "finished", "output": transcript, "reason": ""}
        return {"status": "running", "output": "", "reason": ""}


def _tmux_window_name(engine: str, repo: str, run_id: str) -> str:
    from aegis.connectors.remote_script import _sanitize_window_repo

    return f"{engine}-{_sanitize_window_repo(repo)}-{run_id}"


def _compose_prompt(prompt: str, purpose: str) -> str:
    """Frame a free-form ask for a headless run.

    The run is unattended: nobody can answer a question, and only the final
    message reliably survives into the delivered tail. `start_kimi_run`
    truncates the prompt it writes to the host at 5000 bytes, so the framing
    stays short and the caller's own text goes first-class.
    """
    header = f"Task: {purpose}\n\n" if purpose else ""
    return (
        f"{header}{prompt}\n\n"
        "You are running headless — nobody can answer questions mid-run, so make "
        "reasonable assumptions and state them. When you are done, write your "
        "findings as your FINAL message: a short summary first, then the detail. "
        "Only the tail of your output is delivered back, so put the conclusion at "
        "the end."
    )
