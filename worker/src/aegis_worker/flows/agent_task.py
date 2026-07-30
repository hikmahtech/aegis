"""AgentTaskSweepFlow + AgentTaskFlow — execute agent-assigned Todoist tasks.

The sweep spawns ABANDONED children and never awaits them: a child can sit on
an approval card for days, and Temporal schedules default to overlap=SKIP, so
one unanswered card would starve every later tick (the failure that caused 511
skipped Sentry polls over 41h on 2026-05-29).

Every child ends by completing the task or parking it at @waiting. Eligibility
excludes @waiting, so parking is what removes the task from the pool — without
it the 6h cooldown is an infinite slow loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape as _esc
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.agent_task import extract_service_name, resolve_verb
    from aegis_worker.flows.interaction import InteractionFlow, InteractionFlowInput
    from aegis_worker.shared.retry import (
        ACT_RETRY,
        NO_RETRY,
        TIMEOUT_FAST,
        TIMEOUT_STANDARD,
    )


@dataclass
class AgentTaskSweepConfig:
    agent_id: str  # MUST be first — the run recorder reads it
    max_tasks: int = 3
    cooldown_hours: int = 6
    max_coding: int = 1


@dataclass
class AgentTaskFlowInput:
    agent_id: str  # MUST be first — the run recorder reads it
    # MUST be named todoist_task_id — interceptors._extract_todoist_task_ref
    # reads this exact attribute to populate workflow_runs.todoist_task_ref,
    # which the eligibility cooldown query depends on.
    todoist_task_id: str
    task: dict[str, Any] = field(default_factory=dict)


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
            for task in tasks:
                try:
                    await workflow.start_child_workflow(
                        AgentTaskFlow.run,
                        AgentTaskFlowInput(
                            agent_id=config.agent_id,
                            todoist_task_id=str(task["id"]),
                            task=task,
                        ),
                        id=f"agent-task-{task['id']}",
                        parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                    )
                    spawned += 1
                except WorkflowAlreadyStartedError:
                    continue  # a previous tick's child is still running
                except Exception as exc:  # noqa: BLE001
                    workflow.logger.warning(
                        "agent_task_spawn_failed task_id=%s err=%s",
                        task["id"],
                        str(exc)[:200],
                    )
        except Exception as exc:  # noqa: BLE001
            raise ApplicationError(
                f"agent_task_sweep_failed at step={step}: {exc!r}", non_retryable=True
            ) from exc

        return {"found": len(tasks), "spawned": spawned}


@workflow.defn(name="AgentTaskFlow")
class AgentTaskFlow:
    @workflow.run
    async def run(self, input: AgentTaskFlowInput) -> dict:
        task = input.task
        task_id = input.todoist_task_id
        verb = resolve_verb(task)

        step = "load_task_context"
        try:
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
