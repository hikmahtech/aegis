"""AgentTaskSweepFlow + AgentTaskFlow — execute agent-assigned Todoist tasks.

The sweep spawns ABANDONED children and never awaits them: a child can sit on
an approval card for days, and Temporal schedules default to overlap=SKIP, so
one unanswered card would starve every later tick (the failure that caused 511
skipped Sentry polls over 41h on 2026-05-29).

Every child ends by completing the task or parking it at @waiting. Eligibility
excludes @waiting, so parking is what removes the task from the pool — without
it the 6h cooldown is an infinite slow loop.

The coding verb is the exception to "one child, one shot". A @code task owns a
persistent CLI session (`task_sessions`), and this flow is where it is driven:
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

    from aegis_worker.activities.agent_run import AgentRunActivities
    from aegis_worker.activities.agent_task import extract_service_name, resolve_verb
    from aegis_worker.activities.delivery import DeliveryActivities
    from aegis_worker.flows.agent_run import poll_until_exit
    from aegis_worker.flows.interaction import InteractionFlow, InteractionFlowInput
    from aegis_worker.shared.retry import (
        ACT_RETRY,
        NO_RETRY,
        STANDARD,
        TIMEOUT_FAST,
        TIMEOUT_LLM,
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
# Tail of a finished turn's raw transcript, when it emitted no final message.
_TURN_OUTPUT_TAIL = 6000
# Tail carried by a timeout comment. Deliberately smaller: it is a fragment of
# a run that never concluded, not an answer.
_TURN_TIMEOUT_TAIL = 3000


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
3. A short implementation plan.
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
        except Exception as exc:  # noqa: BLE001
            raise ApplicationError(
                f"agent_task_sweep_failed at step={step}: {exc!r}", non_retryable=True
            ) from exc

        return {"found": len(tasks), "spawned": spawned, "resumed": resumed}

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

        The in-workflow half of `services/task_sessions.dispatch_task_turn`:
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
            verb = resolve_verb(task)

            step = "load_task_context"
            context = await workflow.execute_activity(
                "load_task_context",
                args=[task_id],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )

            if verb == "infra":
                step = "run_infra"
                return await self._run_infra(input, task_id)

            if verb == "email":
                step = "run_email"
                return await self._run_email(input, task_id, context)

            if verb == "finance":
                step = "run_finance"
                return await self._run_finance(input, task_id)

            if verb == "coding":
                step = "run_coding"
                return await self._run_coding(input, task_id, task)

            # Any remaining verb parks the task rather than guessing at it.
            # The loaded source identity (when recovered) rides along in the
            # comment purely as a human debugging aid — no verb-specific
            # behavior depends on it yet.
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

    async def _run_infra(self, input: AgentTaskFlowInput, task_id: str) -> dict:
        """Check live service state; investigate and gate a restart if broken.

        Deliberately does NOT replay alert history: only 12 of 42 open #alert
        tasks have an alert_dedup_index row, and the 30 without one are exactly
        the PROLONGED bulk. Every such title names a service, so asking Docker
        about current state covers all of them.
        """
        title = str(input.task.get("content") or "")
        service = extract_service_name(title)
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

        if health["found"] and health["healthy"]:
            await workflow.execute_activity(
                "comment",
                args=[
                    task_id,
                    input.agent_id,
                    f"`{service}` is healthy now ({health['detail']}) — this alert has "
                    "resolved itself, so I'm closing the task.",
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
        detail = health["detail"] if health["found"] else "not present in the swarm"
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
        **extra: Any,
    ) -> dict:
        """Shared tail for every _run_coding exit: an optional explanation
        comment, then park_task, then the terminal result dict. Every
        _run_coding branch parks — a coding task never auto-completes; even
        an opened PR still needs human review."""
        if comment is not None:
            await workflow.execute_activity(
                "comment",
                args=[task_id, agent_id, comment],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=NO_RETRY,
            )
        await workflow.execute_activity(
            "park_task",
            args=[task_id, reason],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=ACT_RETRY,
        )
        return {"task_id": task_id, "verb": "coding", "status": status, **extra}

    async def _record_turn(self, task_id: str, launched: bool) -> None:
        """Move the session's watermark past the comment this turn consumed.

        EVERY coding exit calls it, including the two that hand the task
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

    async def _deliver(self, agent_id: str, text: str) -> None:
        """Send to the agent's bound channel; never fail the flow over it."""
        try:
            await workflow.execute_activity_method(
                DeliveryActivities.send_message,
                args=[agent_id, text],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=STANDARD,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning(
                "agent_task_delivery_failed agent=%s err=%s", agent_id, str(exc)[:200]
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
        below and `_park_coding` for the rest.
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
                    turns=turns_run,
                )
            session = ensured["session"]

            self._step = "coding:check_task_collision"
            verdict = await workflow.execute_activity(
                "check_task_collision",
                args=[
                    task_id,
                    str(session.get("repo") or ""),
                    str(session.get("session_id") or ""),
                    override,
                ],
                # TIMEOUT_LLM: this check makes a balanced-tier call, and
                # kimi-class calls pass 120s in prod. At 60s the activity would
                # time out and the generic handler would PARK the task — the
                # one outcome the "every failure path returns proceed" contract
                # inside the activity exists to prevent.
                start_to_close_timeout=TIMEOUT_LLM,
                retry_policy=ACT_RETRY,
            )
            call = str(verdict.get("verdict") or "proceed")

            if call == "you_are_in_it":
                held = verdict.get("session") or {}
                name = str(held.get("name") or "unnamed")
                if str(held.get("owner") or "human") == "aegis":
                    # An earlier turn of OUR OWN is still alive — an orphan the
                    # deadline kill did not reach. Nothing has read this
                    # comment and the running turn cannot see it either, so the
                    # watermark must NOT move: leaving the row due is what has
                    # the 15-minute fallback re-dispatch it once the run ends.
                    # No Slack note, because there is no person to tell.
                    workflow.logger.warning(
                        "task_turn_still_running task_id=%s session=%s", task_id, name
                    )
                    return {"task_id": task_id, "verb": "coding", "status": "turn_still_running"}
                # NO Todoist comment and NO park. The comment is already in
                # front of the operator holding this session, so commenting
                # would duplicate it and parking would stamp @waiting on a task
                # somebody is actively working. The watermark DOES move: the
                # comment has been delivered, just not by us.
                await self._record_turn(task_id, False)
                await self._deliver(
                    agent_id,
                    f"You're in the session for task {task_id} ('{name}'); "
                    "your comment is waiting for you there.",
                )
                return {"task_id": task_id, "verb": "coding", "status": "operator_in_session"}

            if call == "hand_to_you":
                await self._record_turn(task_id, False)
                held = verdict.get("session") or {}
                name = str(held.get("name") or "unnamed")
                branch = str(held.get("branch") or "")
                return await self._park_coding(
                    task_id,
                    "operator already on it",
                    status="handed_to_operator",
                    comment=f"You look to be on this already in session '{name}'"
                    + (f" on branch `{branch}`" if branch else "")
                    + ". I'll stay out. Reply `take over` when you want me to proceed.",
                    agent_id=agent_id,
                    turns=turns_run,
                )

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

            # The turn's own verdict, read off the message before the FYI
            # prefix and the take-over footer are wrapped around it.
            status_line = _status_line(body)

            others = verdict.get("sessions") or []
            if others:
                # Judged unrelated, but the operator should still know AEGIS is
                # typing in the same repo they have open.
                body = (
                    "FYI: you have a live session in this repo "
                    f"('{str((others[0] or {}).get('name') or 'unnamed')}'); I'm working "
                    f"in my own worktree at {session.get('worktree_path') or ''}.\n\n"
                ) + body
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
            turns_run += 1

            comments = self._drain()
            if not comments:
                break

        self._step = "coding:park"
        return await self._park_coding(
            task_id,
            "waiting on you",
            status="parked",
            turns=turns_run,
            session_id=str(session.get("session_id") or ""),
            # The last turn's own verdict (`plan`, `question: ...`, `pr: ...`),
            # so `workflow_runs.result_summary` says what the session decided
            # and not merely that a turn happened. "" when it emitted none.
            status_line=status_line,
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
