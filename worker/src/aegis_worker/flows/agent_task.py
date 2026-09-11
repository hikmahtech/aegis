"""AgentTaskSweepFlow + AgentTaskFlow — execute agent-assigned Todoist tasks.

The sweep spawns ABANDONED children and never awaits them: a child can sit on
an approval card for days, and Temporal schedules default to overlap=SKIP, so
one unanswered card would starve every later tick (the failure that caused 511
skipped Sentry polls over 41h on 2026-05-29).

Every child ends by completing the task or parking it at @waiting. Eligibility
excludes @waiting, so parking is what removes the task from the pool — without
it the 6h cooldown is an infinite slow loop.

The coding verb is the exception to "one child, one shot". A @code task owns a
persistent CLI session (`work_sessions`), and this flow is where it is driven:
one turn per batch of user comments, in a per-task worktree, resumed by
session id. A comment that arrives while a turn is running is SIGNALLED into
the running workflow (`comment`), queued, and drained into the next turn — so a
task has at most one workflow (`agent-task-<id>`) and at most one live turn.
The two exits that do not park are deliberate and each carry their reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import escape as _esc
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from aegis.connectors.remote_script import _PROMPT_CAP_BYTES
    from aegis.services.research import task_workflow_id, urls_in

    from aegis_worker.activities.agent_run import AgentRunActivities
    from aegis_worker.activities.agent_task import (
        UNTAGGED,
        VERBS_SETTING,
        extract_service_name,
        resolve_verb,
    )
    from aegis_worker.activities.delivery import DeliveryActivities
    from aegis_worker.flows.agent_chat_reply import AgentChatReplyFlow, AgentChatReplyInput
    from aegis_worker.flows.agent_run import poll_until_exit
    from aegis_worker.flows.interaction import InteractionFlow, InteractionFlowInput
    from aegis_worker.flows.research import ResearchFlow, ResearchInput
    from aegis_worker.shared.retry import (
        ACT_RETRY,
        NO_RETRY,
        STANDARD,
        TIMEOUT_FAST,
        TIMEOUT_LONG,
        TIMEOUT_STANDARD,
    )

# The connector cuts the composed prompt at `_PROMPT_CAP_BYTES` and says
# nothing, and the INSTRUCTIONS — the STATUS contract, the branch rules — are at
# the BOTTOM. So everything variable is capped here first, and the thread then
# takes whatever budget is left over in BYTES, not characters: a cap counted in
# characters is not a cap at all once the conversation stops being ASCII (one
# emoji or CJK character is 3-4 bytes, so a 12 000-character thread can be 40 000
# bytes on its own). The NEWEST lines are kept — a turn needs the last thing the
# user said far more than the first — and the drop is announced, so the model
# knows it is reading a tail rather than the whole story.
_THREAD_NOTE_CAP = 800
_THREAD_RENDER_CAP = 12000
# One pasted stack trace in a description or a comment can outweigh the entire
# rest of the prompt. Cut per field, so no single one can crowd out the others.
_FIELD_CAP = 4000
# Slack for the closing instructions: the budget is computed from the prompt as
# composed, but `title` and the footer text still vary, and running out on the
# last line is the one failure this whole mechanism exists to prevent.
_PROMPT_HEADROOM = 512
_CUT_MARK = " […]"
# The turn's own verdict, which the prompt asks for as the LAST line of the
# final message. Anchored, so a sentence merely mentioning the word does not
# become the recorded status.
_STATUS_RE = re.compile(r"^STATUS:\s*(.+)$")
# The plan block a first turn is asked for: the `PLAN:` marker, then one
# numbered step per line. A marker rather than a heuristic over the whole
# message, because every turn's report is itself a numbered list — parsing
# that would turn 'which files would change' into a checklist.
_PLAN_MARKER_RE = re.compile(r"^\s*PLAN:\s*$")
_PLAN_STEP_RE = re.compile(r"^\s*\d+[.)]\s+(\S.*)$")
_MAX_PLAN_STEPS = 12
# Tail of a finished turn's raw transcript, when it emitted no final message.
_TURN_OUTPUT_TAIL = 6000
# Tail carried by a timeout comment. Deliberately smaller: it is a fragment of
# a run that never concluded, not an answer.
_TURN_TIMEOUT_TAIL = 3000

# #344 changed what a run does after `load_task_context`: the verb now comes
# back from that activity (a setting can change it), `ask` is a new verb, and
# the infra verb asks `plan_infra_task` before anything else. A deploy can land
# while a run is between two activities, and the new worker then replays the
# old history — so the new commands sit behind this patch and the pre-#344
# code stays in the other arm. The runs are short (seconds, except a coding
# turn, whose path this does not touch), so the old arm can go with a
# `workflow.deprecate_patch` once no run started before the deploy is open.
_PATCH_344 = "agent-task-344-verbs-by-kind"
# The verb table before #344, for replaying a run it started. Nothing else
# reads it: a live run takes the verb `load_task_context` returns.
_LEGACY_VERBS = {"#alert": "infra", "#receipt": "finance", "#email": "email"}


def _cut(text: str, cap: int = _FIELD_CAP) -> str:
    """`text`, cut to `cap` characters with a visible mark when it was cut."""
    value = text or ""
    return value if len(value) <= cap else value[:cap] + _CUT_MARK


def _fill_newest_first(lines: list[str], budget: int) -> tuple[list[str], int]:
    """The tail of `lines` that fits in `budget` BYTES, plus how many were cut.

    Newest first while filling, oldest first on the way out: the model reads a
    conversation in order, but the end of it is what the turn has to act on.
    """
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        cost = len(line.encode("utf-8")) + 1
        # `kept and` keeps one line unconditionally: a budget too small for even
        # the newest line must still deliver the last thing the user said.
        if kept and used + cost > budget:
            break
        kept.append(line)
        used += cost
    kept.reverse()
    return kept, len(lines) - len(kept)


def _render_thread(notes: list | None, budget: int = _THREAD_RENDER_CAP) -> str:
    """The task's comment thread as `[<posted_at>] <content>` lines, within `budget`."""
    lines = [
        f"[{str((note or {}).get('posted_at') or '')}] "
        f"{str((note or {}).get('content') or '')[:_THREAD_NOTE_CAP]}"
        for note in (notes or [])
    ]
    if not lines:
        return "(no comments yet)"
    kept, dropped = _fill_newest_first(lines, budget)
    if dropped:
        kept.insert(0, f"[... {dropped} earlier comments omitted]")
    return "\n".join(kept)


def _budget_for(prompt_without_variable_block: str) -> int:
    """Bytes left for the block that has to give way, once everything else is in."""
    return (
        _PROMPT_CAP_BYTES
        - len(prompt_without_variable_block.encode("utf-8"))
        - _PROMPT_HEADROOM
    )


def _thread_root(resp: Any) -> dict | None:
    """`{"channel", "ts"}` for the message comms just sent, or None.

    Three shapes are accepted because `SendResult.to_response()` emits more
    than one: `delivery_ref` spreads the ref's `data` flat alongside `adapter`,
    a `data` sub-object is the documented nesting, and the same keys are
    mirrored at the top level for older dispatch logging. A non-Slack adapter
    carries neither key and correctly yields None — there is no thread to open.
    """
    if not isinstance(resp, dict):
        return None
    ref = resp.get("delivery_ref")
    ref = ref if isinstance(ref, dict) else {}
    nested = ref.get("data")
    for src in (nested if isinstance(nested, dict) else None, ref, resp):
        if isinstance(src, dict) and src.get("channel") and src.get("ts"):
            return {"channel": str(src["channel"]), "ts": str(src["ts"])}
    return None


def _status_line(text: str) -> str:
    """The turn's own `STATUS: <verdict>` line, or `""` when it did not emit one.

    The LAST match wins: the prompt lists the whole contract, so a model that
    quotes the options back before choosing one would otherwise be recorded as
    having answered with the first line of the menu. Not required — a turn ends
    when the process exits, not when a footer appears — which is why an absent
    line is `""` rather than an error.
    """
    for line in reversed((text or "").splitlines()):
        match = _STATUS_RE.match(line)
        if match:
            return match.group(1).strip()
    return ""


def _plan_steps(text: str) -> list[str]:
    """The steps under the LAST `PLAN:` marker, or `[]` when the turn wrote none.

    The last marker wins for the same reason `_status_line` reads the last
    STATUS line: a model that quotes the instructions back before answering
    would otherwise have its example parsed as the plan. Collection stops at
    the first line that is neither a numbered step nor blank, so the footer and
    the STATUS line never become steps.
    """
    lines = (text or "").splitlines()
    start = None
    for index, line in enumerate(lines):
        if _PLAN_MARKER_RE.match(line):
            start = index + 1
    if start is None:
        return []
    steps: list[str] = []
    for line in lines[start:]:
        match = _PLAN_STEP_RE.match(line)
        if match:
            steps.append(match.group(1).strip())
            if len(steps) == _MAX_PLAN_STEPS:
                break
        elif line.strip():
            break
    return steps


def _first_turn_prompt(task_id: str, task: dict, session: dict) -> str:
    """Turn 1: investigate only, and end with a STATUS line.

    Read-only is not a safety rail here so much as a product one — turn 1 runs
    unattended off the sweep, before the user has said anything about this
    task beyond its title, so the only useful output is a plan to react to.

    Composed twice: once without the thread, to learn how many bytes everything
    else costs, and once with a thread rendered to fit what is left. Sizing the
    thread by a fixed constant instead is what let the connector's silent cut
    reach the instructions at the bottom.
    """
    title = str(task.get("content") or "")
    description = _cut(str(task.get("description") or ""))
    branch = str(session.get("branch") or "")
    thread = _render_thread(
        task.get("notes"), _budget_for(_first_turn_body(task_id, title, description, branch, ""))
    )
    return _first_turn_body(task_id, title, description, branch, thread)


def _first_turn_body(
    task_id: str, title: str, description: str, branch: str, thread: str
) -> str:
    return f"""You are working Todoist task {task_id}: {title}

{description}

Comment thread so far (oldest first; AEGIS's own notes carry a `Workflow run:` footer):
{thread}

This is your first turn on this task. Investigate only: read the code, do NOT
modify files, commit, or create branches. Report:
1. What the task is actually asking for.
2. Which files would need to change.
3. A short implementation plan, written as its own block: a line reading
   exactly `PLAN:` followed by one numbered step per line. Each step is one
   piece of work someone could tick off.
4. Anything ambiguous or risky, as questions for the user.

You are in a per-task worktree on branch `{branch}`. Later turns implement here
when the user says so. End your final message with exactly one of:
STATUS: plan
STATUS: question: <what you need from the user>
STATUS: unactionable: <why>"""


def _later_turn_prompt(task_id: str, task: dict, session: dict, comments: list[str]) -> str:
    """Turn 2+: the user's queued replies, quoted, and the session's rules.

    Nothing here re-states the task: the session already holds every earlier
    turn, so repeating the brief would fight its own memory. Several queued
    comments are joined with a blank line — they arrived while one turn ran and
    are answered as one.
    """
    title = str(task.get("content") or "")
    branch = str(session.get("branch") or "")
    blocks = [
        "\n".join(f"> {line}" for line in (_cut(comment).splitlines() or [""]))
        for comment in comments
    ]
    kept, dropped = _fill_newest_first(
        blocks, _budget_for(_later_turn_body(task_id, title, branch, ""))
    )
    if dropped:
        kept.insert(0, f"> [... {dropped} earlier comments omitted]")
    return _later_turn_body(task_id, title, branch, "\n\n".join(kept))


def _later_turn_body(task_id: str, title: str, branch: str, quoted: str) -> str:
    return f"""The user replied on Todoist task {task_id} ({title}):

{quoted}

Act on it. Rules for this session:
- Implement only when the user asks. Commit to branch `{branch}` in this
  worktree, never to the default branch.
- Open a pull request only when the user asks, with `gh pr create --draft`.
- Nobody can answer questions mid-turn; ask them in your final message instead.
End your final message with exactly one of:
STATUS: done
STATUS: waiting: <what you need from the user>
STATUS: pr: <url>
STATUS: unactionable: <why>"""


@dataclass
class AgentTaskSweepConfig:
    agent_id: str  # MUST be first — the run recorder reads it
    max_tasks: int = 3
    cooldown_hours: int = 6
    # Counts NEW and RESUMED coding turns alike. Was 1, when a coding task cost
    # a full investigate-plan-implement-PR arc; a turn is one bounded CLI run,
    # so the ceiling that matters now is how many the host should run at once.
    max_coding: int = 3
    turn_timeout_minutes: int = 60


@dataclass
class AgentTaskFlowInput:
    agent_id: str  # MUST be first — the run recorder reads it
    # MUST be named todoist_task_id — interceptors._extract_todoist_task_ref
    # reads this exact attribute to populate workflow_runs.todoist_task_ref,
    # which the eligibility cooldown query depends on.
    todoist_task_id: str
    task: dict[str, Any] = field(default_factory=dict)
    # The comment that woke this flow. The webhook and the fallback sweep both
    # carry one; the sweep's first turn does not.
    comment: str = ""
    turn_timeout_minutes: int = 60


@workflow.defn(name="AgentTaskSweepFlow")
class AgentTaskSweepFlow:
    @workflow.run
    async def run(self, config: AgentTaskSweepConfig) -> dict:
        step = "find_actionable_tasks"
        try:
            tasks = await workflow.execute_activity(
                "find_actionable_tasks",
                args=[config.max_tasks, config.cooldown_hours, config.max_coding],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=ACT_RETRY,
            )

            step = "spawn_children"
            spawned = 0
            # `max_coding` is a ceiling on TURNS, new and resumed together —
            # they land on the same coding host and the same tmux window cap.
            # Every first turn this loop starts spends one, so the fallback
            # dispatcher below only gets what is left.
            coding_spawned = 0
            for task in tasks:
                try:
                    await workflow.start_child_workflow(
                        AgentTaskFlow.run,
                        AgentTaskFlowInput(
                            agent_id=config.agent_id,
                            todoist_task_id=str(task["id"]),
                            task=task,
                            turn_timeout_minutes=config.turn_timeout_minutes,
                        ),
                        id=f"agent-task-{task['id']}",
                        parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                    )
                    spawned += 1
                    if resolve_verb(task) == "coding":
                        coding_spawned += 1
                except WorkflowAlreadyStartedError:
                    continue  # a previous tick's child is still running
                except Exception as exc:  # noqa: BLE001
                    workflow.logger.warning(
                        "agent_task_spawn_failed task_id=%s err=%s",
                        task["id"],
                        str(exc)[:200],
                    )

            # The fallback for a missed Todoist webhook, and the only path that
            # serves comments posted while the webhook is down. It keys on the
            # session's own `last_turn_at` watermark, NOT the 6h flow cooldown:
            # a reply must not wait six hours because the task ran recently.
            step = "dispatch_due_turns"
            resumed = 0
            budget = max(0, config.max_coding - coding_spawned)
            if budget:
                for row in await self._due_turns(budget):
                    resumed += await self._dispatch_turn(row, config)

            # The registry's liveness cross-check: an operator session that
            # ended without a final `report_progress` is parked once the host
            # no longer lists it. Swallowed for the same reason `_due_turns`
            # is — the children above are already running.
            step = "reconcile_work_sessions"
            await self._reconcile_sessions()
        except Exception as exc:  # noqa: BLE001
            raise ApplicationError(
                f"agent_task_sweep_failed at step={step}: {exc!r}", non_retryable=True
            ) from exc

        return {"found": len(tasks), "spawned": spawned, "resumed": resumed}

    async def _reconcile_sessions(self) -> None:
        try:
            await workflow.execute_activity(
                "reconcile_work_sessions",
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=NO_RETRY,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning("work_sessions_reconcile_failed err=%s", str(exc)[:200])

    async def _due_turns(self, limit: int) -> list:
        """Tasks whose newest user comment is newer than their last turn.

        Swallowed on failure: by the time this runs the spawn loop has already
        started its children, and they are ABANDONED — failing the sweep here
        would report an outage for work that is under way, and the next tick is
        fifteen minutes off either way.
        """
        try:
            return await workflow.execute_activity(
                "find_task_turns_due",
                args=[limit],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning("agent_task_sweep_due_fetch_failed err=%s", str(exc)[:200])
            return []

    async def _dispatch_turn(self, row: dict, config: AgentTaskSweepConfig) -> int:
        """Land one due comment on its task's workflow. Returns 1 on success.

        The in-workflow half of `services/work_sessions.dispatch_task_turn`:
        start the task's single workflow, and if it is already running signal
        the comment into it instead. Every failure is swallowed — one
        unreachable task must not cost the rest of the sweep, and the row stays
        due (its watermark only moves once a turn actually consumes it).
        """
        task_id = str(row.get("task_id") or "")
        comment = str(row.get("comment") or "")
        if not task_id:
            workflow.logger.warning("agent_task_turn_row_has_no_task_id")
            return 0
        wf_id = f"agent-task-{task_id}"
        try:
            await workflow.start_child_workflow(
                AgentTaskFlow.run,
                AgentTaskFlowInput(
                    agent_id=str(row.get("agent_id") or config.agent_id),
                    todoist_task_id=task_id,
                    comment=comment,
                    turn_timeout_minutes=config.turn_timeout_minutes,
                ),
                id=wf_id,
                parent_close_policy=workflow.ParentClosePolicy.ABANDON,
            )
            return 1
        except WorkflowAlreadyStartedError:
            pass
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning(
                "agent_task_turn_start_failed task_id=%s err=%s", task_id, str(exc)[:200]
            )
            return 0
        try:
            await workflow.get_external_workflow_handle(wf_id).signal("comment", comment)
            return 1
        except Exception as exc:  # noqa: BLE001
            # The flow completed between the start attempt and the signal. The
            # comment is still unconsumed, so the next tick starts a fresh
            # workflow for it — 15 minutes later, not never.
            workflow.logger.warning(
                "agent_task_turn_signal_failed task_id=%s err=%s", task_id, str(exc)[:200]
            )
            return 0


@workflow.defn(name="AgentTaskFlow")
class AgentTaskFlow:
    def __init__(self) -> None:
        self._pending: list[str] = []
        # Set by the coding path at every phase, so the generic handler below
        # names the step that actually failed rather than "run_coding".
        self._step = ""

    @workflow.signal
    def comment(self, text: str) -> None:
        """A user comment that arrived while this task's flow was running.

        Queued, never applied here: a signal handler runs between workflow
        tasks, so acting on it would race the turn in flight. The coding loop
        drains the queue after each turn.
        """
        text = (text or "").strip()
        if text and text not in self._pending:
            self._pending.append(text)

    def _drain(self) -> list[str]:
        out, self._pending = self._pending, []
        return out

    @workflow.run
    async def run(self, input: AgentTaskFlowInput) -> dict:
        task = input.task
        task_id = input.todoist_task_id
        verb = "unknown"

        step = "load_task"
        try:
            if not task:
                # The webhook and the fallback sweep carry a task id and a
                # comment, nothing else — load the task rather than trust a
                # payload's copy of it.
                task = await workflow.execute_activity(
                    "load_task",
                    args=[task_id],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=ACT_RETRY,
                )
                if not task:
                    # Deleted between the comment that woke us and this call.
                    # Nothing to park, nothing to comment on.
                    return {"task_id": task_id, "verb": "unknown", "status": "unknown_task"}
                # The other verbs read input.task directly; keep the two views
                # of the task identical rather than threading a second one.
                input.task = task
            verb = resolve_verb(task, _LEGACY_VERBS)

            step = "load_task_context"
            context = await workflow.execute_activity(
                "load_task_context",
                args=[task_id],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
            by_kind = workflow.patched(_PATCH_344)
            if by_kind:
                # The activity resolved it against the `agent_task_verbs`
                # setting, which this workflow cannot read.
                verb = str((context or {}).get("verb") or "unknown")

            if verb == "ask":
                step = "run_ask"
                return await self._run_ask(input, task_id)

            if verb == "research":
                step = "run_research"
                return await self._run_research(input, task_id)

            if verb == "infra":
                step = "run_infra"
                return await self._run_infra(input, task_id, context)

            if verb == "email":
                step = "run_email"
                return await self._run_email(input, task_id, context)

            if verb == "finance":
                step = "run_finance"
                return await self._run_finance(input, task_id)

            if verb == "coding":
                step = "run_coding"
                return await self._run_coding(input, task_id, task)

            if by_kind:
                step = "park_unrouted"
                return await self._park_unrouted(input, task_id, task, verb)

            # Pre-#344, reached only when replaying a run it started: any
            # remaining verb parks the task rather than guessing at it.
            step = "comment"
            source_note = (
                f" (source: {context['external_id']})" if context.get("external_id") else ""
            )
            await workflow.execute_activity(
                "comment",
                args=[
                    task_id,
                    input.agent_id,
                    f"No executor for this task type ({task.get('source_tag') or 'no source tag'})"
                    f"{source_note} — leaving it for you.",
                ],
                # TIMEOUT_STANDARD (60s), not TIMEOUT_FAST (15s): comment()'s
                # own connector call is best-effort internally, but the
                # start-to-close deadline still needs enough room for that
                # call to finish and hand back a caught {"ok": False} rather
                # than have Temporal time out the activity out from under it.
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=NO_RETRY,
            )
            step = "park_task"
            await workflow.execute_activity(
                "park_task",
                args=[task_id, f"no executor for verb={verb}"],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001
            # Every child MUST reach a terminal state — completed or parked —
            # or the task sits in the eligible pool forever, re-picked and
            # re-failed every cooldown window. Best-effort park here (own
            # try/except so a park failure can't mask the original error)
            # before re-raising with step context per repo convention.
            step = self._step or step
            try:
                await workflow.execute_activity(
                    "park_task",
                    args=[task_id, f"agent_task_failed at step={step}: {exc!r}"],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=ACT_RETRY,
                )
            except Exception:  # noqa: BLE001
                workflow.logger.warning(
                    "agent_task_park_on_failure_failed task_id=%s step=%s", task_id, step
                )
            raise ApplicationError(
                f"agent_task_failed at step={step}: {exc!r}", non_retryable=True
            ) from exc

        return {"task_id": task_id, "verb": verb, "status": "parked"}

    async def _run_ask(self, input: AgentTaskFlowInput, task_id: str) -> dict:
        """Hand the task to the agent it is assigned to (#344).

        The executor is `AgentChatReplyFlow`, the one clarify starts when you
        comment on an agent's task: the agent answers in its channel and on the
        task. It is started ABANDONED — a chat turn can take minutes — and the
        task parks now, like a carded verb, so the next tick leaves it alone.
        A later comment on an Inbox task reaches the agent through clarify's
        comment channel, not through this flow.
        """
        ask = await workflow.execute_activity(
            "prepare_agent_ask",
            args=[task_id],
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=ACT_RETRY,
        )
        agent = str(ask.get("agent_id") or "")
        if not agent:
            await workflow.execute_activity(
                "comment",
                args=[task_id, input.agent_id, str(ask.get("comment") or "")],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=NO_RETRY,
            )
            await workflow.execute_activity(
                "park_task",
                args=[task_id, str(ask.get("reason") or "no agent to ask")],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
            return {"task_id": task_id, "verb": "ask", "status": "parked"}
        try:
            await workflow.start_child_workflow(
                AgentChatReplyFlow.run,
                AgentChatReplyInput(
                    target_agent=agent,
                    synthetic_user_message=str(ask.get("message") or ""),
                    thread_id=str(ask.get("thread_id") or f"todoist-task-{task_id}"),
                    task_id=task_id,
                ),
                id=f"agent-task-ask-{task_id}",
                parent_close_policy=workflow.ParentClosePolicy.ABANDON,
            )
        except WorkflowAlreadyStartedError:
            pass  # the last run's ask is still being answered
        await workflow.execute_activity(
            "park_task",
            args=[task_id, f"asked {agent}; the answer lands on the task"],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=ACT_RETRY,
        )
        return {"task_id": task_id, "verb": "ask", "status": "asked", "agent": agent}

    async def _run_research(self, input: AgentTaskFlowInput, task_id: str) -> dict:
        """Research the task's question and post the answer on the task (#509).

        The title is the question; the description is context, and its links
        are read before any search result. `ResearchFlow` runs as a child this
        flow waits on — a quick run is under a minute — and the answer lands as
        ONE task comment with its numbered sources, after which the task parks
        at `@waiting` for the user to read it. Before #509 a `#research` task
        went to `ask`, where the agent could only chat about it.

        The hub problem behind the task is minted first
        (`ensure_problem_for_task`), so the task has a timeline and a session
        registry like a `@code` task. Best-effort: the answer is the job.
        """
        task = input.task or {}
        title = str(task.get("content") or "").strip()
        description = str(task.get("description") or "")
        try:
            await workflow.execute_activity(
                "research_task_problem",
                args=[task_id],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=ACT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 — the timeline is extra
            workflow.logger.warning(
                "research_task_problem_failed task_id=%s err=%s", task_id, str(exc)[:200]
            )
        try:
            result = await workflow.execute_child_workflow(
                ResearchFlow.run,
                ResearchInput(
                    agent_id=input.agent_id or "raphael",
                    question=title,
                    context=_cut(description),
                    seed_urls=urls_in(description, limit=3),
                ),
                id=task_workflow_id(task_id),
            )
        except WorkflowAlreadyStartedError:
            await workflow.execute_activity(
                "park_task",
                args=[task_id, "research is already running for this task"],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
            return {"task_id": task_id, "verb": "research", "status": "parked"}
        result = result if isinstance(result, dict) else {}
        found = result.get("status") == "ok"
        report = str(result.get("report") or "").strip()
        if not report:
            report = (
                "I could not research this one: the task has no title to ask about."
                if not title
                else "I could not research this one."
            )
        await workflow.execute_activity(
            "comment",
            args=[task_id, input.agent_id, report],
            # TIMEOUT_STANDARD, like every comment here: the connector call is
            # best-effort inside and needs room to hand back {"ok": False}.
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=NO_RETRY,
        )
        await workflow.execute_activity(
            "park_task",
            args=[task_id, "research answer posted" if found else "research found no answer"],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=ACT_RETRY,
        )
        return {
            "task_id": task_id,
            "verb": "research",
            "status": "answered" if found else "no_answer",
            "sources": len(result.get("sources") or []),
            "saved": bool(result.get("saved")),
        }

    async def _park_unrouted(
        self, input: AgentTaskFlowInput, task_id: str, task: dict, verb: str
    ) -> dict:
        """Park a task no verb works, once, saying what a person does next.

        `none` is a decision (the tag maps to nothing in the verb table);
        `unknown` is a tag nobody decided about. Both are the human's, and the
        comment says how to change that.
        """
        tag = str(task.get("source_tag") or "")
        what = f"`{tag}` tasks" if tag else "tasks with no source tag"
        key = tag or UNTAGGED
        if verb == "none":
            head = f"Nothing in AEGIS works {what}, so this one is yours."
        else:
            head = f"No lane here takes {what} yet, so this one is yours."
        body = (
            f"{head} Do it and complete the task. If the agent it is assigned to "
            f"should take tasks like this, set `{key}` to `ask` in the "
            f"`{VERBS_SETTING}` setting."
        )
        await workflow.execute_activity(
            "comment",
            args=[task_id, input.agent_id, body],
            # TIMEOUT_STANDARD (60s), not TIMEOUT_FAST (15s): comment()'s own
            # connector call is best-effort internally, but the start-to-close
            # deadline still needs room for it to finish and hand back a caught
            # {"ok": False} rather than be timed out from under it.
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=NO_RETRY,
        )
        await workflow.execute_activity(
            "park_task",
            args=[task_id, f"no verb for {tag or 'an untagged task'} ({verb})"],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=ACT_RETRY,
        )
        return {"task_id": task_id, "verb": verb, "status": "parked"}

    async def _run_infra(self, input: AgentTaskFlowInput, task_id: str, context: dict) -> dict:
        """Check live service state; investigate and gate a restart if broken.

        Since #344 the activity `plan_infra_task` decides from the problem
        behind the task what can be done: a service the swarm runs is checked
        here as before, and anything else (a node, a URL, a group, a subject
        the swarm does not run, an error Sentry reported, AEGIS's own kinds)
        gets a read-only report and parks once.

        The body below the patch check is the pre-#344 verb, kept only to
        replay a run it started. There, a task the problem hub projected
        carried its subject in `context` (`problems.subject`); a hand-written
        one fell back to parsing the title.
        """
        if workflow.patched(_PATCH_344):
            return await self._run_infra_by_kind(input, task_id)
        title = str(input.task.get("content") or "")
        kind = str((context or {}).get("subject_kind") or "")
        if kind and kind != "service":
            # A flow, a purpose, the comms probe, a domain, a post: the hub
            # projected it as a task so the human sees it, but there is no
            # swarm service to check or restart. Say so and park.
            await workflow.execute_activity(
                "comment",
                args=[
                    task_id,
                    input.agent_id,
                    f"This is a {kind} problem ({(context or {}).get('subject')}); "
                    "there is no service to check or restart, so I have no automatic "
                    "action for it.",
                ],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=NO_RETRY,
            )
            await workflow.execute_activity(
                "park_task",
                args=[task_id, f"no automatic action for a {kind} problem"],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
            return {"task_id": task_id, "verb": "infra", "status": "parked", "kind": kind}
        service = str((context or {}).get("subject") or "") or extract_service_name(title)
        if not service:
            await workflow.execute_activity(
                "comment",
                args=[task_id, input.agent_id, "I couldn't tell which service this is about."],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=NO_RETRY,
            )
            await workflow.execute_activity(
                "park_task",
                args=[task_id, "service name not parseable from title"],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
            return {"task_id": task_id, "verb": "infra", "status": "parked"}

        health = await workflow.execute_activity(
            "service_health",
            args=[service],
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=ACT_RETRY,
        )
        return await self._run_service(input, task_id, service, health)

    async def _run_infra_by_kind(self, input: AgentTaskFlowInput, task_id: str) -> dict:
        """The #344 infra verb: the plan first, then the check or the report."""
        plan = await workflow.execute_activity(
            "plan_infra_task",
            args=[task_id, str(input.task.get("content") or "")],
            # A plan runs at most one read against the outside world — one
            # swarm listing or one probe with its own 10s bound — plus a few
            # queries; STANDARD leaves room for either. Read-only, so a retry
            # is safe.
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=ACT_RETRY,
        )
        if plan.get("action") == "service":
            return await self._run_service(
                input, task_id, str(plan.get("service") or ""), dict(plan.get("health") or {})
            )
        await workflow.execute_activity(
            "comment",
            args=[task_id, input.agent_id, str(plan.get("comment") or "")],
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=NO_RETRY,
        )
        await workflow.execute_activity(
            "park_task",
            args=[task_id, str(plan.get("reason") or "infra report")],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=ACT_RETRY,
        )
        return {
            "task_id": task_id,
            "verb": "infra",
            "status": "parked",
            "kind": str(plan.get("kind") or ""),
            "handler": str(plan.get("handler") or ""),
        }

    async def _run_service(
        self, input: AgentTaskFlowInput, task_id: str, service: str, health: dict
    ) -> dict:
        """A swarm service's health now: close a healthy one, card a restart
        for a broken one. Shared by the #344 path and the pre-#344 one, whose
        commands it keeps in the same order."""
        if health.get("found") and health.get("healthy"):
            await workflow.execute_activity(
                "comment",
                args=[
                    task_id,
                    input.agent_id,
                    f"`{service}` is healthy now ({health.get('detail', '')}) — this alert "
                    "has resolved itself, so I'm closing the task.",
                ],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=NO_RETRY,
            )
            await workflow.execute_activity(
                "complete_task",
                args=[task_id],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
            return {"task_id": task_id, "verb": "infra", "status": "resolved", "service": service}

        logs = await workflow.execute_activity(
            "service_logs",
            args=[service, 50],
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=ACT_RETRY,
        )
        detail = health.get("detail", "") if health.get("found") else "not present in the swarm"
        await workflow.execute_activity(
            "comment",
            args=[
                task_id,
                input.agent_id,
                f"`{service}` is still unhealthy ({detail}).\n\n{logs['logs'][:1500]}",
            ],
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=NO_RETRY,
        )

        # Restarting is a write, so it needs human approval before this flow
        # would ever execute it — an InteractionFlow card + post_resolve
        # activity, same pattern as social_publish.py/review.py.
        try:
            await workflow.start_child_workflow(
                InteractionFlow.run,
                InteractionFlowInput(
                    agent_id=input.agent_id,
                    kind="choice",
                    origin="agent_task_infra",
                    prompt=(
                        f"🔧 <b>{_esc(service)}</b> is unhealthy ({detail}).\n\n"
                        "Restart it?"
                    ),
                    options={"approve": "🔄 Restart", "skip": "⏭️ Leave it"},
                    timeout_seconds=86400,
                    timeout_policy="archive",
                    metadata={
                        "task_id": task_id,
                        "service": service,
                        "agent_id": input.agent_id,
                    },
                    post_resolve_activity="apply_restart_approval",
                ),
                id=f"agent-task-restart-{task_id}",
                parent_close_policy=workflow.ParentClosePolicy.ABANDON,
            )
        except WorkflowAlreadyStartedError:
            pass  # a previous run's card is still open

        # Park now: the card's post_resolve hook owns the outcome from here, and
        # parking keeps the task out of the next tick's selection meanwhile.
        await workflow.execute_activity(
            "park_task",
            args=[task_id, f"awaiting restart approval for {service}"],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=ACT_RETRY,
        )
        return {"task_id": task_id, "verb": "infra", "status": "carded", "service": service}

    async def _run_email(
        self, input: AgentTaskFlowInput, task_id: str, context: dict
    ) -> dict:
        """Archive notification mail; park anything needing a human reply."""
        title = str(input.task.get("content") or "")
        outcome = await workflow.execute_activity(
            "triage_email",
            args=[task_id, title, context.get("gmail_message_id", "")],
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=ACT_RETRY,
        )

        if outcome["action"] == "archived":
            await workflow.execute_activity(
                "comment",
                args=[
                    task_id,
                    input.agent_id,
                    "This is an automated notification, not an action — archived it "
                    f"in {outcome['account']} and closing the task.",
                ],
                # TIMEOUT_STANDARD, not TIMEOUT_FAST: comment()'s own connector
                # call needs enough room to finish and hand back a caught
                # {"ok": False} rather than have Temporal cancel it mid-call
                # (same reasoning as every other comment() call site here).
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=NO_RETRY,
            )
            await workflow.execute_activity(
                "complete_task",
                args=[task_id],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
            return {"task_id": task_id, "verb": "email", "status": "archived"}

        reason = (
            "needs a reply, and I can't send mail (scope is gmail.modify)"
            if outcome["action"] == "needs_human"
            else "I couldn't find this message in any connected account"
        )
        await workflow.execute_activity(
            "comment",
            args=[task_id, input.agent_id, f"Leaving this one for you — {reason}."],
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=NO_RETRY,
        )
        await workflow.execute_activity(
            "park_task",
            args=[task_id, f"email {outcome['action']}"],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=ACT_RETRY,
        )
        return {"task_id": task_id, "verb": "email", "status": "parked"}

    async def _run_finance(self, input: AgentTaskFlowInput, task_id: str) -> dict:
        """Gather merchant context and put the decision to the user.

        No autonomous write: whether a charge is legitimate is the user's
        call, so this verb only assembles history and cards a decision.
        """
        title = str(input.task.get("content") or "")
        history = await workflow.execute_activity(
            "merchant_history",
            args=[title, 6],
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=ACT_RETRY,
        )
        if not history["merchant"]:
            await workflow.execute_activity(
                "comment",
                args=[task_id, input.agent_id, "I couldn't tell which merchant this is about."],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=NO_RETRY,
            )
            await workflow.execute_activity(
                "park_task",
                args=[task_id, "merchant not parseable from title"],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
            return {"task_id": task_id, "verb": "finance", "status": "parked"}

        await workflow.execute_activity(
            "comment",
            args=[
                task_id,
                input.agent_id,
                f"Prior charges for {history['merchant']}: {history['summary']}",
            ],
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=NO_RETRY,
        )
        try:
            await workflow.start_child_workflow(
                InteractionFlow.run,
                InteractionFlowInput(
                    agent_id=input.agent_id,
                    kind="choice",
                    origin="agent_task_finance",
                    prompt=(
                        f"💳 <b>{_esc(history['merchant'])}</b>\n\n{title}\n\n"
                        f"History: {history['summary']}\n\nIs this expected?"
                    ),
                    options={"expected": "✅ Expected", "investigate": "🔍 Investigate"},
                    timeout_seconds=86400,
                    timeout_policy="archive",
                    metadata={
                        "task_id": task_id,
                        "agent_id": input.agent_id,
                        "merchant": history["merchant"],
                    },
                    post_resolve_activity="apply_finance_decision",
                ),
                id=f"agent-task-finance-{task_id}",
                parent_close_policy=workflow.ParentClosePolicy.ABANDON,
            )
        except WorkflowAlreadyStartedError:
            pass

        await workflow.execute_activity(
            "park_task",
            args=[task_id, "awaiting finance decision"],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=ACT_RETRY,
        )
        return {"task_id": task_id, "verb": "finance", "status": "carded"}

    async def _park_coding(
        self,
        task_id: str,
        reason: str,
        *,
        status: str = "parked",
        comment: str | None = None,
        agent_id: str | None = None,
        sess: dict | None = None,
        title: str = "",
        **extra: Any,
    ) -> dict:
        """Shared tail for every _run_coding exit: an optional explanation
        comment, then park_task, then the terminal result dict. Every
        _run_coding branch parks — a coding task never auto-completes; even
        an opened PR still needs human review.

        `sess` opts the comment into the task's Slack thread. It is a separate
        argument rather than something read off the flow because the branches
        that exit before a session is resolved still have a comment worth
        mirroring — and because a comment nobody passes a session for is one
        this tail must not try to deliver.
        """
        if comment is not None:
            await workflow.execute_activity(
                "comment",
                args=[task_id, agent_id, comment],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=NO_RETRY,
            )
            if sess is not None:
                await self._mirror_to_thread(task_id, str(agent_id or ""), sess, comment, title)
        await workflow.execute_activity(
            "park_task",
            args=[task_id, reason],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=ACT_RETRY,
        )
        return {"task_id": task_id, "verb": "coding", "status": status, **extra}

    async def _record_turn(self, task_id: str, launched: bool) -> None:
        """Move the session's watermark past the comment this turn consumed.

        EVERY coding exit calls it, including the one that hands the task
        straight back — the comment has been dealt with, and the 15-minute
        fallback sweep keys on this watermark, so skipping it re-dispatches the
        same comment for ever.
        """
        await workflow.execute_activity(
            "record_task_turn",
            args=[task_id, launched],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=ACT_RETRY,
        )

    async def _count_launched_turn(self, task_id: str) -> None:
        """Count a turn that actually started. Never retried, never fatal.

        `turns` decides resume-vs-create and numbers the take-over footer, and
        this call happens with a live CLI session already running. A retry would
        count that one session twice, and an exception would fail the flow out
        from under a turn nobody is left to poll — so both are refused. The
        watermark this shares an activity with has already moved.
        """
        try:
            await workflow.execute_activity(
                "record_task_turn",
                args=[task_id, True],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning(
                "task_turn_not_counted task_id=%s err=%s", task_id, str(exc)[:200]
            )

    async def _deliver(
        self,
        agent_id: str,
        text: str,
        thread_ref: dict | None = None,
        thread_overflow: bool = False,
    ) -> dict | None:
        """Send to the agent's bound channel; never fail the flow over it.

        Returns the comms response — which carries the sent message's ref — or
        None when the send failed, so a caller opening a thread can tell "no
        root came back" from "there is a root".

        `thread_overflow` marks a task message: a turn's output is far longer
        than Slack's chunk limit, so without it a message that OPENS a thread
        finishes as loose posts in the channel, and a reply under one of those
        is not recognised as the task's.
        """
        try:
            return await workflow.execute_activity_method(
                DeliveryActivities.send_message,
                args=[agent_id, text, 0, thread_ref, thread_overflow],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=STANDARD,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning(
                "agent_task_delivery_failed agent=%s err=%s", agent_id, str(exc)[:200]
            )
            return None

    async def _mirror_to_thread(
        self, task_id: str, agent_id: str, sess: dict, text: str, title: str = ""
    ) -> None:
        """Mirror one task message into the task's Slack thread.

        Todoist is where a task message is RECORDED; the thread is where the
        operator reads it and answers, and an answer typed in the thread comes
        back as the next turn's comment. So every comment the coding path
        writes goes through here.

        The first message for a task has no root to post under, so it becomes
        one: it leads with the task id and title, and the ref comms hands back
        is stored on the session row AND on `sess`, so the rest of this loop
        threads without waiting for the next re-read of the row.

        Best-effort throughout. The comment is already posted by the time this
        runs, and a comms outage must cost the notification, never the turn.
        """
        ref = sess.get("slack_ref") if isinstance(sess.get("slack_ref"), dict) else None
        header = f"Task {task_id}: {title}".strip()
        body = text if ref else f"{header}\n\n{text}"

        resp = await self._deliver(agent_id, body, ref, thread_overflow=True)
        if ref is not None:
            return
        root = _thread_root(resp)
        if root is None:
            return
        sess["slack_ref"] = root
        try:
            await workflow.execute_activity(
                "set_task_slack_ref",
                args=[task_id, root],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning(
                "task_slack_ref_not_stored task_id=%s err=%s", task_id, str(exc)[:200]
            )

    async def _run_coding(self, input: AgentTaskFlowInput, task_id: str, task: dict) -> dict:
        """Drive the task's persistent coding session, one turn per comment batch.

        The loop is the feature. A turn runs; its output is posted as a task
        comment; the pending queue is drained; a non-empty drain is the next
        turn's comments and an empty one parks the task at @waiting. So a
        comment posted while a turn was running folds into the next turn
        instead of colliding with the running one or waiting for the sweep.

        The session is re-read from `ensure_task_session` on EVERY iteration
        rather than reused: `turns` decides resume-vs-create, and the worktree
        check is what self-heals a tree removed out of band between turns.

        Two exits do not park, and both are deliberate — see `you_are_in_it`
        and `turn_still_running` below, and `_park_coding` for the rest.
        """
        agent_id = input.agent_id
        timeout_min = max(1, int(input.turn_timeout_minutes or 60))

        if "notes" not in task:
            # The sweep's first-turn path hands over `find_actionable_tasks`
            # rows, which are task COLUMNS only. The comment thread is the
            # session's context — the operator's instructions usually live
            # there, not in the title — so load it rather than open turn 1 on
            # an empty conversation. `run()`'s webhook path already has it.
            self._step = "coding:load_task"
            loaded = await workflow.execute_activity(
                "load_task",
                args=[task_id],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
            if loaded:
                task = loaded
        title = str(task.get("content") or "")

        # The comment that woke this flow, plus anything that arrived before we
        # got here. Deduped: the webhook and the fallback sweep can both carry
        # the same note, and answering it twice in one prompt is noise.
        comments = [input.comment.strip()] if input.comment.strip() else []
        comments += [c for c in self._drain() if c not in comments]
        turns_run = 0
        status_line = ""
        session: dict = {}

        while True:
            # `take over` is the operator overruling the same-task check for
            # this turn only (rule 4). Rule 1 still applies — a comment must
            # not be able to authorise driving a session someone is sitting in.
            override = any(c.lower().startswith("take over") for c in comments)

            self._step = "coding:ensure_task_session"
            ensured = await workflow.execute_activity(
                "ensure_task_session",
                args=[task_id, agent_id, task, comments[-1] if comments else ""],
                # TIMEOUT_LONG, not STANDARD: the first turn on a big repo runs
                # `git worktree add` over SSH, which can outlast 60s. Timing it
                # out would surface as a park, not as a retry.
                start_to_close_timeout=TIMEOUT_LONG,
                retry_policy=ACT_RETRY,
            )
            status = str(ensured.get("status") or "")
            if status == "candidates":
                await self._record_turn(task_id, False)
                names = ", ".join(
                    str(c.get("github_repo") or "")
                    for c in (ensured.get("candidates") or [])
                    if c.get("github_repo")
                )
                return await self._park_coding(
                    task_id,
                    "repo ambiguous",
                    status="repo_ambiguous",
                    comment="I can't tell which repository this is about. Reply with "
                    f"one of: {names}",
                    agent_id=agent_id,
                    sess=ensured.get("session") or {},
                    title=title,
                    turns=turns_run,
                )
            if status != "ready" or not ensured.get("session"):
                # No repo and nothing to choose between. The session row still
                # exists, so the next comment reaches this flow and can name one.
                await self._record_turn(task_id, False)
                error = str(ensured.get("error") or "")
                return await self._park_coding(
                    task_id,
                    "repo unresolved",
                    comment="I couldn't work out which repository this task is about"
                    + (f": {error}" if error else "")
                    + ", so I haven't touched anything.",
                    agent_id=agent_id,
                    sess=ensured.get("session") or {},
                    title=title,
                    turns=turns_run,
                )
            session = ensured["session"]

            self._step = "coding:check_task_collision"
            verdict = await workflow.execute_activity(
                "check_task_collision",
                args=[task_id, override],
                # A registry read plus one SSH liveness probe. Every failure
                # path inside returns `proceed`; a timeout here would surface
                # as a park instead, which is why the probe is bounded.
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=ACT_RETRY,
            )
            call = str(verdict.get("verdict") or "proceed")

            if call == "turn_still_running":
                # An earlier turn of OUR OWN is still alive — an orphan the
                # deadline kill did not reach. Nothing has read this comment
                # and the running turn cannot see it either, so the watermark
                # must NOT move: leaving the row due is what has the 15-minute
                # fallback re-dispatch it once the run ends. No Slack note,
                # because there is no person to tell.
                workflow.logger.warning(
                    "task_turn_still_running task_id=%s reason=%s",
                    task_id,
                    str(verdict.get("reason") or ""),
                )
                return {"task_id": task_id, "verb": "coding", "status": "turn_still_running"}

            if call == "you_are_in_it":
                held = verdict.get("session") or {}
                name = str(held.get("name") or "unnamed")
                # NO Todoist comment and NO park. The operator's own session
                # reported itself active on this task, so the comment is
                # already in front of them: commenting would duplicate it and
                # parking would stamp @waiting on a task somebody is actively
                # working. The watermark DOES move: the comment has been
                # delivered, just not by us.
                await self._record_turn(task_id, False)
                # Into the task's thread when it has one, the agent channel
                # otherwise. Never opens a thread: this is a note ABOUT the
                # task, and a root nobody replies under is a dead thread.
                existing = session.get("slack_ref")
                root = existing if isinstance(existing, dict) else None
                await self._deliver(
                    agent_id,
                    f"You're in the session for task {task_id} ('{name}'); "
                    "your comment is waiting for you there.",
                    root,
                    thread_overflow=root is not None,
                )
                return {"task_id": task_id, "verb": "coding", "status": "operator_in_session"}

            # Read BEFORE the watermark bump below: `turns` is what decides
            # whether this turn creates the session or resumes it, and what
            # numbers the footer the operator takes over with.
            first = int(session.get("turns") or 0) == 0
            turn_no = int(session.get("turns") or 0) + 1
            prompt = (
                _first_turn_prompt(task_id, task, session)
                if first
                else _later_turn_prompt(task_id, task, session, comments)
            )

            # The WATERMARK moves before the launch: a launch that fails still
            # consumed the comment, and a watermark left behind would have the
            # fallback sweep re-dispatch it every 15 minutes for ever. The turn
            # COUNT does not — see below.
            self._step = "coding:record_task_turn"
            await self._record_turn(task_id, False)

            self._step = "coding:launch_task_turn"
            launched = await workflow.execute_activity(
                "launch_task_turn",
                args=[
                    session,
                    prompt,
                    agent_id,
                    not first,
                    f"task {task_id}: {title[:60]}",
                    timeout_min,
                ],
                start_to_close_timeout=TIMEOUT_LONG,
                # NO_RETRY, as everywhere a CLI session is started: a retry is a
                # SECOND billed session racing the first one's writes.
                retry_policy=NO_RETRY,
            )
            output_file = str(launched.get("output_file") or "")
            host = str(launched.get("host") or "")
            if str(launched.get("status") or "") != "running":
                return await self._park_coding(
                    task_id,
                    "turn failed to start",
                    status="launch_failed",
                    comment="I couldn't start a turn on this: "
                    f"{launched.get('error') or 'unknown error'}",
                    agent_id=agent_id,
                    sess=session,
                    title=title,
                    turns=turns_run,
                )
            # Counted only now that a session demonstrably exists. Counting it
            # before the launch made a FAILED first launch leave `turns` at 1,
            # and every later turn then ran `--resume` against a session that
            # was never created — a task poisoned by one bad launch.
            await self._count_launched_turn(task_id)

            self._step = "coding:poll"
            outcome = await poll_until_exit(
                output_file=output_file,
                host=host,
                deadline_s=timeout_min * 60,
                launched_at=workflow.now(),
            )
            if str(outcome.get("status") or "") == "timeout":
                self._step = "coding:kill_task_turn"
                await self._kill_turn(output_file, host)
                # `poll_until_exit` returns NO output on a timeout — it reports
                # the deadline and nothing else — so the tail has to be fetched
                # here or the comment is a bare "it stopped". Fetched AFTER the
                # kill, so what the operator reads is the last thing the run
                # wrote. "asked to stop", never "was stopped": the kill is
                # `fuser -k` and may have found nothing to kill.
                self._step = "coding:timeout_tail"
                tail = (await self._fetch_tail(output_file, host))[-_TURN_TIMEOUT_TAIL:]
                body = (
                    f"Turn was asked to stop after {timeout_min} min (deadline). "
                    f"Output so far:\n\n{tail or '(no output captured)'}"
                )
            else:
                body = (
                    str(outcome.get("final") or "")
                    or str(outcome.get("output") or "")[-_TURN_OUTPUT_TAIL:]
                    or str(outcome.get("reason") or "")
                    or "no output"
                )

            # The turn's own verdict, read off the message before the
            # take-over footer is wrapped around it.
            status_line = _status_line(body)
            # A plan becomes a checklist on the task: the projector opens one
            # subtask per step, and whoever does the work ticks them off with
            # `report_progress(step_done=N)`. Best-effort — the plan is already
            # posted as the comment below either way.
            steps = _plan_steps(body)
            if len(steps) > 1:
                self._step = "coding:record_plan"
                await self._record_plan(task_id, steps, body, turn_no)

            session_id = str(session.get("session_id") or "")
            body += (
                f"\n\nSession: {session_id} · turn {turn_no}\n"
                f"Take over: cd {session.get('worktree_path') or ''} && "
                f"claude --resume {session_id}"
            )

            self._step = "coding:comment"
            await workflow.execute_activity(
                "comment",
                args=[task_id, agent_id, body],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=NO_RETRY,
            )
            # No `_step` marker: this cannot be the step a failure is reported
            # at (it swallows everything), and leaving one behind would
            # misattribute the next thing that does fail.
            await self._mirror_to_thread(task_id, agent_id, session, body, title)
            turns_run += 1

            comments = self._drain()
            if not comments:
                break

        self._step = "coding:park"
        return await self._park_coding(
            task_id,
            # The reason lands on the session row (`park_task` writes it), so
            # it carries the turn's own verdict: "waiting on you: pr: #12" is
            # what a session opened on the task later reads first.
            f"waiting on you: {status_line}" if status_line else "waiting on you",
            status="parked",
            turns=turns_run,
            session_id=str(session.get("session_id") or ""),
            # The last turn's own verdict (`plan`, `question: ...`, `pr: ...`),
            # so `workflow_runs.result_summary` says what the session decided
            # and not merely that a turn happened. "" when it emitted none.
            status_line=status_line,
        )

    async def _record_plan(
        self, task_id: str, steps: list[str], text: str, turn_no: int
    ) -> None:
        """Hand the turn's plan to the hub, which turns it into subtasks.

        Swallowed on failure and never retried into a second checklist: the
        turn has run and its plan is about to be posted as a comment, so a hub
        that is unreachable costs the checklist and nothing else.
        """
        try:
            await workflow.execute_activity(
                "record_plan",
                args=[
                    {
                        "task_id": task_id,
                        "steps": steps,
                        "text": text[:2000],
                        "external_id": f"plan:{workflow.info().workflow_id}:{turn_no}",
                    }
                ],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=NO_RETRY,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning(
                "task_plan_not_recorded task_id=%s err=%s", task_id, str(exc)[:200]
            )

    async def _kill_turn(self, output_file: str, host: str) -> None:
        """Ask a timed-out turn to stop. Failure is logged, never fatal.

        The kill is best-effort by construction — `kill_task_turn` reports that
        the command RAN, not that a process died — so the flow already cannot
        rely on it. Letting an exception here bury the timeout comment would
        cost the operator the one signal that says what happened.
        """
        try:
            await workflow.execute_activity(
                "kill_task_turn",
                args=[output_file, host],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=STANDARD,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning(
                "agent_task_kill_failed output_file=%s err=%s", output_file, str(exc)[:200]
            )

    async def _fetch_tail(self, output_file: str, host: str) -> str:
        """Whatever a timed-out turn managed to write, for its comment.

        `probe_alive=False` on purpose: the run has just been asked to stop, so
        a liveness probe would answer "dead" and the check would report a
        failure we already know about. All this call is here for is the
        transcript. Failure returns "" — a deadline comment without a tail is
        worth far more than no comment at all.
        """
        if not output_file:
            return ""
        try:
            check = await workflow.execute_activity_method(
                AgentRunActivities.check_agent_run,
                args=[output_file, host, False],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=STANDARD,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning(
                "agent_task_tail_fetch_failed output_file=%s err=%s", output_file, str(exc)[:200]
            )
            return ""
        return str(check.get("output") or "")
