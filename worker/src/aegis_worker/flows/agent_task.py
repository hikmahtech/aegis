"""AgentTaskSweepFlow + AgentTaskFlow — execute agent-assigned Todoist tasks.

The sweep spawns ABANDONED children and never awaits them: a child can sit on
an approval card for days, and Temporal schedules default to overlap=SKIP, so
one unanswered card would starve every later tick (the failure that caused 511
skipped Sentry polls over 41h on 2026-05-29).

Every child ends by completing the task or parking it at @waiting. Eligibility
excludes @waiting, so parking is what removes the task from the pool — without
it the 6h cooldown is an infinite slow loop.

The coding verb is gone: the development lane (coding sessions, the repo
registry, the coding host) moved to the Development vertical (a2-development).
An untagged `@code` task still resolves to `coding`, and parks once with a note
saying so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape as _esc
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from aegis.errors import error_text, logged_failure
    from aegis.services.research import task_workflow_id, urls_in

    from aegis_worker.activities.agent_task import (
        UNTAGGED,
        VERBS_SETTING,
        resolve_verb,
    )
    from aegis_worker.flows.agent_chat_reply import AgentChatReplyFlow, AgentChatReplyInput
    from aegis_worker.flows.interaction import InteractionFlow, InteractionFlowInput
    from aegis_worker.flows.research import ResearchFlow, ResearchInput
    from aegis_worker.shared.retry import (
        ACT_RETRY,
        NO_RETRY,
        TIMEOUT_FAST,
        TIMEOUT_STANDARD,
    )

# One pasted stack trace in a description can outweigh the rest of a
# research question's context. Cut per field.
_FIELD_CAP = 4000
_CUT_MARK = " […]"

# Retired `workflow.patched` ids. The old branches are gone; the markers
# stay one release longer as `workflow.deprecate_patch`, because a run that
# RECORDED one is wedged by a worker whose code no longer mentions it at all
# ("[TMPRL1100] Non-deprecated patch marker encountered"). Drop the calls and
# these ids in the release after next — see #614.
# One call covers both of #344's old sites: the SDK records a marker once per
# id per run, and the first site is on every path that reached the second.
_PATCH_344 = "agent-task-344-verbs-by-kind"

# The `infra` verb is gone: the infra lane moved to the DevOps vertical
# (a2-devops), and `agent_task_verbs` no longer offers it (`#alert` maps to
# null). A run recorded before this change may have taken the infra branch, so
# `_run_infra_by_kind` and `_run_service` stay for its replay; a new run that
# still reads `infra` (it cannot, the verb table refuses it) parks as
# unrouted. `plan_infra_task` stays registered as a stub for the same release.
# Retire the branch, the stub and these methods the #614 way.
PATCH_DROP_INFRA_VERB = "agent-task-drop-infra-verb"

# The coding lane left v1 (the Development vertical, a2-development). A sweep
# recorded before this change ran the fallback turn dispatcher and the session
# reconcile after spawning its children; a new tick ends after the spawn. The
# two activities those steps call (`find_task_turns_due`,
# `reconcile_work_sessions`) stay one release as no-op stubs, and
# `AgentTaskSweepConfig.max_coding` / `turn_timeout_minutes` stay for the
# legacy branch only. Retire them all the #614 way.
PATCH_DROP_CODING_SWEEP = "agent-task-sweep-drop-coding"

# What an untagged `@code` task is told when the sweep reaches it.
CODING_MOVED_COMMENT = (
    "AEGIS v1 no longer runs coding tasks: the development lane moved to the "
    "Development vertical. Hand this one to it, or do it yourself and complete the task."
)


def _cut(text: str, cap: int = _FIELD_CAP) -> str:
    """`text`, cut to `cap` characters with a visible mark when it was cut."""
    value = text or ""
    return value if len(value) <= cap else value[:cap] + _CUT_MARK


@dataclass
class AgentTaskSweepConfig:
    agent_id: str  # MUST be first — the run recorder reads it
    max_tasks: int = 3
    cooldown_hours: int = 6
    # Read only by the legacy branch (PATCH_DROP_CODING_SWEEP): the coding
    # turn budget and a turn's deadline. A sweep recorded before the change
    # replays with its own recorded values; a new tick ignores both. Remove
    # them with the patch.
    max_coding: int = 0
    turn_timeout_minutes: int = 60


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
        resumed = 0
        try:
            tasks = await workflow.execute_activity(
                "find_actionable_tasks",
                args=[config.max_tasks, config.cooldown_hours],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=ACT_RETRY,
            )

            step = "spawn_children"
            spawned = 0
            # Counted for the legacy branch only (the turn budget it spends).
            coding_spawned = 0
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
                    if resolve_verb(task) == "coding":
                        coding_spawned += 1
                except WorkflowAlreadyStartedError:
                    continue  # a previous tick's child is still running
                except Exception as exc:  # noqa: BLE001
                    workflow.logger.warning(
                        "agent_task_spawn_failed task_id=%s err=%s",
                        task["id"],
                        error_text(exc),
                    )

            if not workflow.patched(PATCH_DROP_CODING_SWEEP):
                # Legacy replay only: the coding lane's fallback turn
                # dispatcher and the session reconcile.
                step = "dispatch_due_turns"
                budget = max(0, config.max_coding - coding_spawned)
                if budget:
                    for row in await self._due_turns(budget):
                        resumed += await self._dispatch_turn(row, config)
                step = "reconcile_work_sessions"
                await self._reconcile_sessions()
        except Exception as exc:  # noqa: BLE001
            raise ApplicationError(
                f"agent_task_sweep_failed at step={step}: {exc!r}", non_retryable=True
            ) from exc

        return {"found": len(tasks), "spawned": spawned, "resumed": resumed}

    async def _reconcile_sessions(self) -> None:
        """Legacy replay only (PATCH_DROP_CODING_SWEEP)."""
        with logged_failure("work_sessions_reconcile_failed", logger=workflow.logger):
            await workflow.execute_activity(
                "reconcile_work_sessions",
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=NO_RETRY,
            )

    async def _due_turns(self, limit: int) -> list:
        """Legacy replay only (PATCH_DROP_CODING_SWEEP). Swallowed on failure,
        as it always was: the children above are already running."""
        try:
            return await workflow.execute_activity(
                "find_task_turns_due",
                args=[limit],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning("agent_task_sweep_due_fetch_failed err=%s", error_text(exc))
            return []

    async def _dispatch_turn(self, row: dict, config: AgentTaskSweepConfig) -> int:
        """Legacy replay only (PATCH_DROP_CODING_SWEEP): start the task's
        workflow, or signal the comment into it when it is already running.
        The command shape is the old one; the child it starts runs today's
        code, which parks a task it is handed without a body."""
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
                ),
                id=wf_id,
                parent_close_policy=workflow.ParentClosePolicy.ABANDON,
            )
            return 1
        except WorkflowAlreadyStartedError:
            pass
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning(
                "agent_task_turn_start_failed task_id=%s err=%s", task_id, error_text(exc)
            )
            return 0
        try:
            await workflow.get_external_workflow_handle(wf_id).signal("comment", comment)
            return 1
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning(
                "agent_task_turn_signal_failed task_id=%s err=%s", task_id, error_text(exc)
            )
            return 0


@workflow.defn(name="AgentTaskFlow")
class AgentTaskFlow:
    @workflow.run
    async def run(self, input: AgentTaskFlowInput) -> dict:
        task = input.task
        task_id = input.todoist_task_id

        if not task:
            # Only the coding lane started this flow without the task (the
            # Todoist webhook and the fallback turn dispatcher, both gone).
            # Nothing to work and nothing to park.
            return {"task_id": task_id, "verb": "unknown", "status": "no_task"}

        step = "load_task_context"
        try:
            context = await workflow.execute_activity(
                "load_task_context",
                args=[task_id],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
            # deprecate_patch: remove after the next release, see #614
            workflow.deprecate_patch(_PATCH_344)
            # The activity resolved it against the `agent_task_verbs` setting,
            # which this workflow cannot read.
            verb = str((context or {}).get("verb") or "unknown")

            if verb == "ask":
                step = "run_ask"
                return await self._run_ask(input, task_id)

            if verb == "research":
                step = "run_research"
                return await self._run_research(input, task_id)

            if verb == "infra" and not workflow.patched(PATCH_DROP_INFRA_VERB):
                # Legacy replay only; see PATCH_DROP_INFRA_VERB.
                step = "run_infra"
                return await self._run_infra_by_kind(input, task_id)

            if verb == "email":
                step = "run_email"
                return await self._run_email(input, task_id, context)

            if verb == "finance":
                step = "run_finance"
                return await self._run_finance(input, task_id)

            if verb == "coding":
                step = "park_coding"
                return await self._park_coding(input, task_id)

            step = "park_unrouted"
            return await self._park_unrouted(input, task_id, task, verb)
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

    async def _park_coding(self, input: AgentTaskFlowInput, task_id: str) -> dict:
        """An untagged `@code` task: say the coding lane moved, and park it once."""
        await workflow.execute_activity(
            "comment",
            args=[task_id, input.agent_id, CODING_MOVED_COMMENT],
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=NO_RETRY,
        )
        await workflow.execute_activity(
            "park_task",
            args=[task_id, "coding lane moved to the Development vertical"],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=ACT_RETRY,
        )
        return {"task_id": task_id, "verb": "coding", "status": "parked"}

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
        problem: dict = {}
        try:
            got = await workflow.execute_activity(
                "research_task_problem",
                args=[task_id],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=ACT_RETRY,
            )
            problem = got if isinstance(got, dict) else {}
        except Exception as exc:  # noqa: BLE001 — the timeline is extra
            workflow.logger.warning(
                "research_task_problem_failed task_id=%s err=%s", task_id, error_text(exc)
            )
        question, context, seeds = title, _cut(description), urls_in(description, limit=3)
        if problem.get("class") == "topic" and problem.get("topic"):
            # A topic's task (#513) is titled "<topic>: new items worth a
            # look", which is not a question: researched verbatim it cost a
            # smart-tier run to answer nothing. Research the topic itself, with
            # the round's items as the context and their links read first.
            topic = str(problem["topic"])
            items = [i for i in problem.get("items") or [] if isinstance(i, dict)]
            question = f"What is new in {topic}, and what in these items matters?"
            context = _cut(
                "\n".join(f"- {i.get('title') or ''} ({i.get('url') or ''})" for i in items)
            )
            seeds = [str(i["url"]) for i in items if i.get("url")][:3]
        try:
            result = await workflow.execute_child_workflow(
                ResearchFlow.run,
                ResearchInput(
                    # "" lets the child resolve the `research` tag holder.
                    agent_id=input.agent_id or "",
                    question=question,
                    context=context,
                    seed_urls=seeds,
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

    async def _run_infra_by_kind(self, input: AgentTaskFlowInput, task_id: str) -> dict:
        """The retired #344 infra verb, kept for replay (PATCH_DROP_INFRA_VERB)."""
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
