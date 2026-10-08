"""Chat tools that start, stop and inspect agent runs on the coding host.

Two of the three hand a long job to something outside the chat turn — an
`AgentRunFlow`, or a kill of one already in flight — and the third reports
what is open there. None of them can finish inside a chat turn's budget, so
each returns as soon as the work is handed over. (`aegis_self_diagnose` and
`investigate_resource` left with the infra lane for the DevOps vertical.)
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import uuid4

import asyncpg
import structlog
from pydantic import Field

from aegis.agent_tags import GENERALIST_TAG
from aegis.errors import error_text
from aegis.services.agents import resolve_tag
from aegis.services.tools.base import ToolContext
from aegis.services.tools.registry import aegis_tool

logger = structlog.get_logger()


# Watch-window bounds for `dispatch_agent_run`, mirroring its tool schema.
# 30 matches `AgentRunInput.timeout_minutes`'s own default. A GATED run is
# different in kind: it blocks on a human for up to 9 minutes per approval
# card, so 3-4 questions exhaust a 30-minute window while the CLI is still
# raising them — the flow stops watching a run that is working fine.
_RUN_TIMEOUT_MIN_MINUTES = 5
_RUN_TIMEOUT_MAX_MINUTES = 240
_UNGATED_TIMEOUT_MINUTES = 30
_GATED_TIMEOUT_MINUTES = 120


def _run_timeout_minutes(raw: Any, gated: bool) -> int:
    """The caller's `timeout_minutes`, clamped to the schema's bounds.

    Unparseable or absent ⇒ the default for this kind of run. Clamped rather
    than rejected: the schema already refuses out-of-range values on the
    validated paths, and a dispatch is not worth failing over a stray number.
    """
    default = _GATED_TIMEOUT_MINUTES if gated else _UNGATED_TIMEOUT_MINUTES
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        return default
    return max(_RUN_TIMEOUT_MIN_MINUTES, min(_RUN_TIMEOUT_MAX_MINUTES, minutes))


@aegis_tool
async def _exec_dispatch_agent_run(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    prompt: str,
    repo: str | None = None,
    engine: Literal["claude", "kimi"] | None = None,
    purpose: str | None = None,
    gated: bool | None = None,
    timeout_minutes: Annotated[int, Field(ge=5, le=240)] | None = None,
    todoist_task_id: str | None = None,
) -> str:
    """Dispatch a LONG-RUNNING background agent run (a headless claude/kimi CLI session on the coding host) and return immediately — the result arrives in this channel later, typically in several minutes. Use it for heavy multi-step work you cannot finish in this reply: investigating a codebase, researching something end-to-end, analysing data, drafting a large change. Do NOT use it for a quick question you can answer yourself or with a read-only tool — this costs minutes and a full agent session. Write the prompt as a complete standalone brief: the run cannot see this conversation and nobody can answer its questions mid-run.

    Args:
        prompt: The full standalone brief for the run: what to do, what to look at, what to report back.
        repo: Optional workspace-relative checkout to run in, e.g. 'bcp' or 'acme/bcp'. Omit for work that needs no repo (a shared scratch workspace is used).
        engine: Optional engine override. Omit to let repo/org routing decide.
        purpose: Short human label for the run, e.g. 'audit bcp retry logic'. Shown in the result header.
        gated: Require human approval for mutating actions during the run; approval cards land in your channel. Use it when the run can change things (write files, run commands, open PRs) or will read untrusted content. Requires the claude engine.
        timeout_minutes: Optional watch window in minutes (5-240). Omit for the default (30, or 120 for a gated run, which spends most of its time waiting for approvals). A timeout never kills the run — it only stops watching it.
        todoist_task_id: Optional Todoist task this run is working. Ties the run to that task so asking twice cannot start a second session on the same work.

    Returns:
        Spawns AgentRunFlow — the heavy lane. Fire-and-forget.

    Core never imports worker code, so the flow is started by NAME with a plain
    dict arg (Temporal's converter fills the AgentRunInput dataclass), exactly
    like the agent-reply trigger route and `_exec_investigate_resource`. The
    converter IGNORES an unknown key, so every key below must match a field on
    `AgentRunInput` — a typo silently takes that field's default (a mistyped
    `gated` is an ungated run reporting success). `tests/worker/
    test_dataclass_payload_seams.py` asserts the two sides still agree.

    `timeout_minutes` is the watch window, not a kill switch. A gated run
    spends most of it waiting on humans (each card holds up to 9 min), so an
    unset value defaults to `_GATED_TIMEOUT_MINUTES` rather than the flow's 30.
    """
    from temporalio.exceptions import WorkflowAlreadyStartedError

    if not ctx.temporal_client:
        return "Can't dispatch: Temporal client not available."
    prompt = (prompt or "").strip()
    if not prompt:
        return "Can't dispatch: prompt is required."
    engine = (engine or "").strip().lower()
    if engine and engine not in ("claude", "kimi"):
        return f"Can't dispatch: unknown engine '{engine}' — use 'claude' or 'kimi', or omit it."
    # No calling agent: the generalist runs it, never an example id (#579).
    agent_id = ctx.agent_id or await resolve_tag(pool, GENERALIST_TAG)
    if not agent_id:
        return "Can't dispatch: no agent to run it as — no active agent holds the gtd tag."
    gated = bool(gated)
    # A run tied to a Todoist task gets a DETERMINISTIC workflow id, so asking
    # twice for the same task is refused by Temporal instead of starting a
    # second CLI session on the same work. The untied case keeps a random id:
    # two "look into X" asks are two legitimate runs.
    task_id = (todoist_task_id or "").strip()
    # The run id is chosen HERE and handed to the connector (#640). It used to
    # make its own at launch, so the confirmation said agent-run-a5828448, the
    # result said run=b580fd58, nothing linked the two, and the id the model
    # was given could not stop the run.
    run_id = uuid4().hex[:8]
    workflow_id = f"agent-run-task-{task_id}" if task_id else f"agent-run-{run_id}"
    # Name the engine the run will get, never "auto": a model handed a gap
    # fills it, and on 2026-09-21 it told the owner "Kimi" for a claude run.
    # The lookup is the one the launch makes; with no coding host to ask, say
    # so instead of guessing.
    engine_label = engine
    connector = getattr(ctx, "remote_script_connector", None)
    if not engine_label and connector is not None:
        try:
            engine_label = await connector.routed_engine()
        except Exception as exc:  # noqa: BLE001 — the dispatch does not hinge on the label
            logger.warning("dispatch_agent_run_engine_lookup_failed", error=error_text(exc))
    try:
        await ctx.temporal_client.start_workflow(
            "AgentRunFlow",
            {
                "agent_id": agent_id,
                "prompt": prompt,
                "repo": (repo or "").strip() or None,
                "engine": engine,
                "purpose": (purpose or "").strip(),
                "gated": gated,
                "timeout_minutes": _run_timeout_minutes(timeout_minutes, gated),
                "run_id": run_id,
            },
            id=workflow_id,
            task_queue="aegis-main",
        )
    except WorkflowAlreadyStartedError:
        # Its run id is the earlier dispatch's, which this call cannot see.
        return (
            f"A run for that task is already in flight ({workflow_id}). To start "
            "over, find its run id with list_coding_sessions and stop it with "
            "stop_agent_run."
        )
    except Exception as exc:  # noqa: BLE001 — a dispatch failure is a chat answer, not a crash
        logger.warning("dispatch_agent_run_failed", workflow_id=workflow_id, error=error_text(exc))
        return f"Couldn't dispatch the agent run: {error_text(exc)}"
    logger.info(
        "dispatch_agent_run_started", workflow_id=workflow_id, run_id=run_id, agent_id=agent_id
    )
    engine_part = (
        f"engine={engine_label}"
        if engine_label
        else "engine not known yet (the launch decides; the result header names it)"
    )
    return (
        f"Dispatched agent run {run_id} ({engine_part}). The result will land in this "
        f"channel with the header `run={run_id}`; stop it with stop_agent_run(run_id="
        f"'{run_id}'). Tell the user only what this line says: the run has not started "
        "yet, so there is no outcome to report."
    )


@aegis_tool
async def _exec_stop_agent_run(pool: asyncpg.Pool, ctx: ToolContext, *, run_id: str) -> str:
    """Stop a running background agent run on the coding host by killing its session. Use it when a run is doing the wrong thing, duplicates work you are already doing yourself, or is no longer wanted. The run's workspace is cleaned up automatically afterwards. Stopping is not reversible — the work in progress is lost — so prefer letting a nearly-finished run complete.

    Args:
        run_id: The run id, as given in the dispatch confirmation or shown by list_coding_sessions.

    Returns:
        The flow is not signalled: its next poll sees the process gone, reports
        the run as failed and removes the worktree. So stopping is one action
        here, not a two-sided handshake that could half-complete. "Not found"
        is reported plainly rather than as an error — the run may have already
        finished, or have been launched detached with no window at all.
    """
    run_id = (run_id or "").strip()
    if not run_id:
        return "Can't stop: run_id is required (the id in the dispatch message, or from list_coding_sessions)."
    connector = getattr(ctx, "remote_script_connector", None)
    if connector is None:
        return "The coding host is not configured."
    result = await connector.stop_coding_run(run_id)
    if result.get("stopped"):
        return (
            f"Stopped run {run_id} (tmux window `{result.get('window')}`). "
            "Its worktree is cleaned up when the flow next polls."
        )
    reason = result.get("reason") or "unknown"
    if reason == "not_found":
        return (
            f"No live tmux window for run {run_id} — it has probably finished already, "
            "or was launched detached."
        )
    if reason == "invalid_run_id":
        return f"'{run_id}' is not a valid run id."
    return f"Couldn't stop run {run_id}: {reason}."


@aegis_tool(empty_required=True)
async def _exec_list_coding_sessions(pool: asyncpg.Pool, ctx: ToolContext) -> str:
    """List coding-CLI sessions currently open on the coding host, across every configured account. Read-only. Shows which repo each session is in and whether it is busy, and marks AEGIS's own runs as owner=aegis. Use it to answer 'what is running on the coding host?', or to check before asking for a coding run whether someone is already working in that repo.

    Returns:
        Reports `disabled` distinctly from "nothing running": an operator
        reading an empty list must be able to tell "nobody is working" from
        "the feature was never switched on".
    """
    if not ctx.remote_script_connector:
        return "The coding host is not configured."
    result = await ctx.remote_script_connector.list_coding_sessions()
    status = result.get("status")
    if status == "disabled":
        return "Session inventory is disabled for this coding host (coding.inventory.enabled)."
    sessions = result.get("sessions") or []
    errors = result.get("errors") or []
    if not sessions and status != "ok":
        detail = "; ".join(f"{e.get('account', '?')}: {e.get('error', '')}" for e in errors)
        return f"Could not read the session inventory. {detail}".strip()
    if not sessions:
        return "No coding sessions are open on the coding host."
    lines = [
        f"- {s.get('name') or s.get('session_id')} "
        f"[{s.get('account')}] {s.get('repo') or s.get('cwd')} "
        f"— {s.get('status')} ({s.get('owner')})"
        for s in sessions
    ]
    if errors:
        lines.append(f"({len(errors)} account(s) could not be read)")
    return "\n".join(lines)
