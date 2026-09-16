"""Chat tools that start, stop and inspect agent runs on the coding host.

Four of the five hand a long job to something outside the chat turn — a kimi
run against AEGIS's own checkout, an `AlertInvestigationFlow` against a
registered repo, an `AgentRunFlow`, or a kill of one already in flight — and
the fifth reports what is open there. None of them can finish inside a chat
turn's budget, so each either polls under its own deadline or returns as soon
as the work is handed over.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
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


_KIMI_STATUS_RE_CHAT = re.compile(r"^STATUS:\s*\S+", re.MULTILINE)
_AEGIS_SELF_DIAGNOSE_MAX_WAIT = 480  # 8 minutes; leaves headroom under synthesize_reply's 600s
_AEGIS_SELF_DIAGNOSE_POLL = 15  # poll interval in seconds
# Hard per-fetch cap so a hung SSH `cat` can't stall the poll loop past the
# deadline; above the connector's internal 15s so a normal read isn't preempted.
_AEGIS_SELF_DIAGNOSE_FETCH_TIMEOUT = 20
_AEGIS_SELF_DIAGNOSE_OUTPUT_CAP = 8 * 1024  # last N chars returned to the LLM


def _slugify_issue(text: str, max_len: int = 32) -> str:
    """Stable slug for `aegis-fix/<slug>` branch names. Lowercase a-z0-9-, capped."""
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (base or "issue")[:max_len].strip("-") or "issue"


def _build_aegis_self_diagnose_prompt(issue: str, mode: str, fix_branch: str) -> str:
    """Compose the kimi prompt for AEGIS self-investigation / self-fix.

    The convention mirrors `_build_alert_investigation_prompt` in the worker's
    alerts.py but is workspace-aware (AEGIS's own source) and adds the
    branch+PR convention for `fix` mode. The STATUS footer is REQUIRED so
    the polling loop terminates cleanly.
    """
    prompt = (
        "You are pandora-as-kimi, debugging AEGIS itself. Workspace: this repo, "
        "rooted at the current directory. Use Shell, Read, Glob, and other tools to "
        "gather concrete evidence — never speculate.\n\n"
        f"Mode: {mode}\nIssue:\n{issue}\n\n"
        "Steps:\n"
        "1. Identify the relevant files / flows / activities.\n"
        "2. Read enough source to understand the actual behavior.\n"
        "3. Diagnose the root cause (or confirm the user's hypothesis).\n"
    )
    if mode == "fix":
        prompt += (
            f"4. Implement the fix. Create branch `{fix_branch}`, commit with a clear "
            "message, push to origin, then `gh pr create --draft` with a summary + "
            "test plan. Output a line: `BRANCH: aegis:<branch_name>` and "
            "`PR: <url>`. Do NOT commit speculative or untested changes. "
            "Do NOT commit directly to main.\n"
        )
    else:
        prompt += (
            "4. Propose the fix as a unified diff or file-targeted change list in your "
            "final assistant message. Do NOT modify files in this mode.\n"
        )
    prompt += (
        "5. The LAST line of your output MUST be exactly one of:\n"
        "     STATUS: investigated\n"
        "     STATUS: proposed\n"
        "     STATUS: shipped\n"
        "     STATUS: insufficient_evidence: <what you could not check>\n"
        "     STATUS: unactionable: <why this isn't fixable>\n"
    )
    return prompt


@aegis_tool
async def _exec_aegis_self_diagnose(
    pool: asyncpg.Pool, ctx: ToolContext, *, issue: str, mode: Literal["investigate", "fix"]
) -> str:
    """Investigate / fix AEGIS itself by driving the kimi CLI over SSH on node-a against the AEGIS source checkout. The kimi run has full Shell / Read / Glob / WriteFile permissions. Use this when the user asks about AEGIS's own behavior, bugs, or improvements. For code FIXES, kimi MUST create a branch (`aegis-fix/<slug>`), commit, push, and open a PR via `gh pr create` — never direct-commit to main. The tool waits up to 8 minutes for kimi's STATUS footer; if the run exceeds that, the partial output is returned with a `still_running` flag so the user can ask for a follow-up.

    Args:
        issue: What kimi should investigate or fix. Be concrete: file paths, error messages, observed behavior, what 'good' looks like.
        mode: `investigate` = read-only RCA + propose fix in chat. `fix` = also commit + push + open PR. Both modes give kimi the full toolset; the prompt enforces the convention.

    Returns:
        Three phases: start_kimi_run with the baked self-diagnose prompt; poll
        fetch_kimi_run_output every 15s until the STATUS footer or 8 minutes;
        return the parsed transcript (last 8KB) + run_id + status flag.
    """
    issue = (issue or "").strip()
    mode = mode or "investigate"
    if not issue:
        return json.dumps({"error": "issue is required"})
    if mode not in {"investigate", "fix"}:
        return json.dumps({"error": "mode must be 'investigate' or 'fix'"})
    if not ctx.remote_script_connector:
        return json.dumps({"error": "remote_script connector not available"})
    if ctx.settings is None:
        return json.dumps({"error": "settings not threaded into ToolContext"})

    settings = ctx.settings
    # DB-first coding config (infra registry row with coding.enabled) wins over
    # env settings; the try/except keeps plain test doubles (MagicMock
    # connectors without an awaitable coding_settings) working.
    coding: dict = {}
    try:
        coding = await ctx.remote_script_connector.coding_settings()
    except Exception:  # noqa: BLE001 — connector without the accessor
        coding = {}
    repo = coding.get("self_repo_path") or settings.aegis_self_repo_path or "personal/aegis"
    kimi_binary = coding.get("kimi_binary") or settings.kimi_cli_binary_path
    fix_branch = f"aegis-fix/{_slugify_issue(issue)}"
    prompt = _build_aegis_self_diagnose_prompt(issue, mode, fix_branch)

    # Single wall-clock deadline covering BOTH launch (start_kimi_run's SSH
    # round-trips) AND the poll loop, so the executor's TOTAL runtime stays
    # under the outer tool-timeout guillotine (_TOOL_TIMEOUT_OVERRIDES). The old
    # code started this clock only after launch, so slow SSH setup plus the
    # loop's terminal poll could overshoot the override — the tool then timed
    # out and the run_id was lost (3/3 prod timeouts, agent=pandoras-actor,
    # 2026-07-15).
    deadline = time.monotonic() + _AEGIS_SELF_DIAGNOSE_MAX_WAIT

    try:
        run_result = await ctx.remote_script_connector.start_kimi_run(
            repo, prompt, kimi_binary=kimi_binary
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("aegis_self_diagnose_start_failed", error=error_text(exc, 500))
        return json.dumps({"error": f"kimi launch failed: {error_text(exc)}"})

    if run_result.get("status") == "failed":
        return json.dumps({"error": run_result.get("error", "kimi launch failed")})

    output_file = run_result.get("output_file", "")
    run_id = run_result.get("run_id", "")
    latest_raw = ""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # Hard-bound the fetch so one hung SSH `cat` degrades to a skipped poll
        # instead of blocking the loop past the deadline (and the guillotine).
        try:
            raw = await asyncio.wait_for(
                ctx.remote_script_connector.fetch_kimi_run_output(
                    output_file, host=run_result.get("host", "")
                ),
                timeout=_AEGIS_SELF_DIAGNOSE_FETCH_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 — a probe failure is a skipped poll, not a tool timeout
            logger.warning("aegis_self_diagnose_fetch_failed", run_id=run_id, error=error_text(exc, 500))
            raw = None
        if raw:
            latest_raw = raw
            if _KIMI_STATUS_RE_CHAT.search(raw):
                return json.dumps(
                    {
                        "status": "completed",
                        "run_id": run_id,
                        "output_file": output_file,
                        "transcript": raw[-_AEGIS_SELF_DIAGNOSE_OUTPUT_CAP:],
                        "fix_branch": fix_branch if mode == "fix" else None,
                    }
                )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(_AEGIS_SELF_DIAGNOSE_POLL, remaining))
    return json.dumps(
        {
            "status": "still_running",
            "run_id": run_id,
            "output_file": output_file,
            "transcript": latest_raw[-_AEGIS_SELF_DIAGNOSE_OUTPUT_CAP:]
            if latest_raw
            else "(no output yet — kimi may still be initialising)",
            "note": "Run exceeded 8min. Use the run_id / output_file to follow up.",
            "fix_branch": fix_branch if mode == "fix" else None,
        }
    )


@aegis_tool
async def _exec_investigate_resource(
    pool: asyncpg.Pool, ctx: ToolContext, *, repo: str, focus: str
) -> str:
    """Kick off a full code investigation (kimi over SSH) of a registered repository this task concerns. Use when the task or the user's comment clearly pertains to a specific repo in the resource list. Runs asynchronously: the findings and a fix-approval (Gate-2) card are posted back to THIS Todoist task in a few minutes. Only works when replying on a Todoist task (not a DM).

    Args:
        repo: The resource/repo the task is about, e.g. 'bcp'.
        focus: One line: what to investigate, derived from the task title and the user's comment.

    Returns:
        Spawns AlertInvestigationFlow. Pandora-only, comment-channel-only.
        Fire-and-forget: the durable flow posts the verdict + plain-text kimi
        transcript back to the current Todoist task and fires the Gate-2
        approval card. Returns immediately. source='todoist-chat' (non-Jira)
        keeps Gate-2 ON and kimi fix-capable; todoist_task_id attaches the run
        to this card AND bypasses the alert-signature dedup.
    """
    repo = (repo or "").strip()
    focus = (focus or "").strip()
    if not repo or not focus:
        return json.dumps({"error": "repo and focus are required"})
    task_id = (ctx.task_id or "").strip()
    if not task_id:
        return json.dumps(
            {"error": "investigate_resource only works when replying on a Todoist task (not a DM)"}
        )
    if not ctx.temporal_client:
        return json.dumps({"error": "temporal client not available"})

    # Validate repo against registered resources (basename of github_repo, or path).
    try:
        rows = await pool.fetch(
            "SELECT metadata->>'github_repo' AS gh, metadata->>'path' AS rp FROM resources"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("investigate_resource_lookup_failed", error=error_text(exc))
        return json.dumps({"error": f"resource lookup failed: {error_text(exc)}"})
    target = repo.lower()
    matched = False
    available: set[str] = set()
    for r in rows:
        gh = (r["gh"] or "").strip()
        rp = (r["rp"] or "").strip()
        gh_base = gh.rsplit("/", 1)[-1].lower() if gh else ""
        # path is workspace-relative and may be nested ("acme/bcp") —
        # match on its basename too.
        rp_base = rp.rsplit("/", 1)[-1].lower() if rp else ""
        if gh_base:
            available.add(gh_base)
        elif rp_base:
            available.add(rp_base)
        if target and target in {gh_base, rp.lower(), rp_base, gh.lower()}:
            matched = True
    if not matched:
        return json.dumps({"error": f"unknown repo '{repo}'", "available_repos": sorted(available)})

    from temporalio.exceptions import WorkflowAlreadyStartedError

    workflow_id = f"chat-investigate-{task_id}"
    alert = {
        "title": focus[:200],
        "description": f"{focus}\n\n(triggered by pandora on Todoist task {task_id})"[:2000],
        "source": "todoist-chat",
        "service": repo,
        "severity": "normal",
        "fingerprint": f"chat-investigate-{task_id}",
        "labels": {"alertname": focus[:100], "service": repo},
        "requires_approval": False,
        "todoist_task_id": task_id,
    }
    # The task's own problem, when it has a live one (#472): named, the flow's
    # step 0 records on it; unnamed, it ingested a fresh event, created a
    # second problem and linked this task to both. A closed problem is
    # history, so that task starts fresh. The lookup only enriches the run,
    # so a failure starts it the old way rather than not at all.
    try:
        from aegis.services.hub import find_problem_for_task

        problem = await find_problem_for_task(pool, task_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("investigate_resource_problem_lookup_failed", error=error_text(exc))
        problem = None
    if problem is not None and problem["closed_at"] is None:
        alert["problem_id"] = problem["id"]
    try:
        await ctx.temporal_client.start_workflow(
            "AlertInvestigationFlow",
            alert,
            id=workflow_id,
            task_queue="aegis-main",
        )
    except WorkflowAlreadyStartedError:
        return json.dumps({"status": "already_investigating", "workflow_id": workflow_id, "repo": repo})
    except Exception as exc:  # noqa: BLE001
        logger.warning("investigate_resource_spawn_failed", repo=repo, error=error_text(exc))
        return json.dumps({"error": f"failed to start investigation: {error_text(exc)}"})
    return json.dumps({"status": "investigation_started", "workflow_id": workflow_id, "repo": repo})


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
    workflow_id = f"agent-run-task-{task_id}" if task_id else f"agent-run-{uuid4().hex[:8]}"
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
            },
            id=workflow_id,
            task_queue="aegis-main",
        )
    except WorkflowAlreadyStartedError:
        return (
            f"A run for that task is already in flight ({workflow_id}). "
            "Stop it with stop_agent_run if you want to start over."
        )
    except Exception as exc:  # noqa: BLE001 — a dispatch failure is a chat answer, not a crash
        logger.warning("dispatch_agent_run_failed", workflow_id=workflow_id, error=error_text(exc))
        return f"Couldn't dispatch the agent run: {error_text(exc)}"
    logger.info("dispatch_agent_run_started", workflow_id=workflow_id, agent_id=agent_id)
    return (
        f"Dispatched agent run {workflow_id} ({engine or 'auto'}) — "
        "results will land in this channel."
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
