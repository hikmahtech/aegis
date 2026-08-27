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
from datetime import timedelta
from html import escape as _esc
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.agent_task import extract_service_name, resolve_verb
    from aegis_worker.activities.alert_governance import (
        CreateGithubPrInput,
        StagePendingPrInput,
    )
    from aegis_worker.flows.alert_investigation import _build_repo_confirm_prompt
    from aegis_worker.flows.interaction import InteractionFlow, InteractionFlowInput
    from aegis_worker.shared.retry import (
        ACT_RETRY,
        NO_RETRY,
        RETRY_ONCE,
        TIMEOUT_CLAUDE,
        TIMEOUT_FAST,
        TIMEOUT_LONG,
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

            if verb == "coding":
                step = "run_coding"
                return await self._run_coding(input, task_id)

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

    async def _run_coding(self, input: AgentTaskFlowInput, task_id: str) -> dict:
        """Resolve the repo, investigate read-only, gate the plan, implement on
        approval, then gate the PR. Every exit path parks EXCEPT a repo-busy
        skip, which must leave the task untouched so it stays in the eligible
        pool — a coding task never auto-completes; even an opened PR still needs
        human review."""
        setup = await self._investigate_coding_task(input, task_id)
        if "plan" not in setup:
            return setup  # early exit: parked, or a repo-busy skip that must not park
        repo, plan = setup["repo"], setup["plan"]

        # AWAIT the plan card. Safe because the sweep spawned this workflow
        # ABANDONED — blocking here cannot starve later ticks.
        plan_card = await workflow.execute_child_workflow(
            InteractionFlow.run,
            InteractionFlowInput(
                agent_id=input.agent_id,
                kind="choice",
                origin="agent_task_coding_plan",
                prompt=(
                    # _esc() every user/agent-derived string inside an HTML tag —
                    # repo-wide convention (_run_infra, _run_finance,
                    # calendar_ingest, receipt_ingest, gmail_ingest all do this).
                    # A literal `<` or `&` in a task title otherwise breaks the
                    # comms html_to_mrkdwn parse.
                    f"🛠 <b>{_esc(str(input.task.get('content') or ''))}</b>\n\n"
                    f"Repo: <code>{_esc(repo['github_repo'])}</code>\n\n"
                    f"{_esc(plan[:1200])}\n\n"
                    "Implement this?"
                ),
                options={"approve": "✅ Implement", "skip": "⏭️ Not now"},
                timeout_seconds=172800,
                timeout_policy="archive",
            ),
            id=f"agent-task-plan-{task_id}",
        )
        if (plan_card.response or {}).get("value") != "approve":
            return await self._park_coding(task_id, "plan not approved", status="plan_declined")

        return await self._implement_and_open_pr(input, task_id, repo, plan)

    async def _confirm_repo_gate0(
        self, input: AgentTaskFlowInput, task_id: str, candidates: list[dict]
    ) -> dict | None:
        """Tier 3: resolve_task_repo's tiers 1-2 didn't confidently resolve a
        repo but did surface candidates — ask rather than guess. Mirrors
        alert_investigation.py's Gate-0 repo-confirm card (same
        _build_repo_confirm_prompt + numbered candidate menu), blocking here
        because AgentTaskFlow is already an ABANDONED child of the sweep (safe
        to await, same reasoning as the plan/PR cards above).

        Returns the resolved repo dict on a confirmed pick, or None on
        decline/timeout — the caller then parks exactly as it would for a
        fully-unresolved repo."""
        top = candidates[:5]
        if not top:
            return None
        options = {
            str(i): f"{i + 1}. 📦 {c.get('resource_title') or c.get('github_repo') or '?'}"
            for i, c in enumerate(top)
        }
        options["none"] = "❌ None of these / cancel"
        prompt = _build_repo_confirm_prompt(
            title=str(input.task.get("content") or ""),
            source="agent_task",
            severity="",
            service="",
            description=str(input.task.get("description") or ""),
            task_id=task_id,
            candidates=top,
        )
        picked = await workflow.execute_child_workflow(
            InteractionFlow.run,
            InteractionFlowInput(
                agent_id=input.agent_id,
                kind="choice",
                origin="agent_task_repo_confirm",
                prompt=prompt,
                options=options,
                timeout_seconds=86400,
                timeout_policy="archive",
            ),
            id=f"agent-task-repo-confirm-{task_id}",
        )
        if getattr(picked, "status", None) == "archived":
            return None
        picked_val = ((picked.response or {}).get("value") or "").strip()
        if not picked_val.isdigit() or int(picked_val) >= len(top):
            return None
        chosen = top[int(picked_val)]
        chosen_repo = chosen.get("github_repo") or ""
        return {
            "github_repo": chosen_repo,
            "repo_path": chosen.get("resource_path") or chosen_repo.split("/")[-1],
            "source": "user_confirmed",
            "candidates": [],
        }

    async def _investigate_coding_task(self, input: AgentTaskFlowInput, task_id: str) -> dict:
        """Phase 1: resolve the repo, run a read-only investigation, and post
        the plan as a comment. Returns the terminal result dict directly on
        any early exit (no repo / investigation failed / empty transcript);
        otherwise returns {"repo": repo, "plan": plan} for the plan-approval
        gate in _run_coding."""
        repo = await workflow.execute_activity(
            "resolve_task_repo",
            args=[input.task],
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=ACT_RETRY,
        )
        # Tier 3: tiers 1-2 didn't confidently resolve a repo but did surface
        # candidates — ask rather than guess (Gate-0 confirm card).
        if not repo["github_repo"] and repo.get("candidates"):
            confirmed = await self._confirm_repo_gate0(input, task_id, repo["candidates"])
            if confirmed is not None:
                repo = confirmed
        if not repo["github_repo"]:
            return await self._park_coding(
                task_id,
                "repo unresolved",
                comment="I couldn't work out which repository this task is about, so I "
                "haven't touched anything.",
                agent_id=input.agent_id,
            )

        investigation = await workflow.execute_activity(
            "run_task_investigation",
            args=[
                task_id,
                str(input.task.get("content") or ""),
                str(input.task.get("description") or ""),
                repo["repo_path"],
                repo["github_repo"],
            ],
            start_to_close_timeout=TIMEOUT_LONG,
            retry_policy=RETRY_ONCE,
        )
        # A transient collision must NOT park: park_task stamps @waiting, which
        # removes the task from find_actionable_tasks' pool until something
        # unparks it, so a busy afternoon would retire the task for good.
        # Returning without "plan" makes _run_coding exit; the cooldown on this
        # workflow's terminal run row defers the retry.
        if investigation.get("status") == "skipped":
            return {"status": "skipped", "reason": "repo_busy", "task_id": task_id}

        if investigation.get("status") == "failed":
            return await self._park_coding(
                task_id,
                "coding run failed to start",
                comment="I couldn't start a coding run for this.",
                agent_id=input.agent_id,
            )

        collected = await workflow.execute_activity(
            "collect_coding_run",
            args=[investigation.get("output_file", ""), investigation.get("host", "")],
            start_to_close_timeout=TIMEOUT_CLAUDE,
            retry_policy=NO_RETRY,
            heartbeat_timeout=timedelta(minutes=2),
        )
        plan = collected.get("transcript", "")
        if not plan:
            return await self._park_coding(
                task_id,
                "empty investigation transcript",
                comment="The investigation produced no usable output.",
                agent_id=input.agent_id,
            )

        await workflow.execute_activity(
            "comment",
            args=[task_id, input.agent_id, f"Investigation in `{repo['github_repo']}`:\n\n{plan}"],
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=NO_RETRY,
        )
        return {"repo": repo, "plan": plan}

    async def _implement_and_open_pr(
        self, input: AgentTaskFlowInput, task_id: str, repo: dict, plan: str
    ) -> dict:
        """Phase 2: implement the approved plan, then gate opening a PR."""
        implementation = await workflow.execute_activity(
            "run_task_implementation",
            args=[
                task_id,
                str(input.task.get("content") or ""),
                str(input.task.get("description") or ""),
                plan,
                repo["repo_path"],
                repo["github_repo"],
            ],
            start_to_close_timeout=TIMEOUT_LONG,
            retry_policy=NO_RETRY,
        )
        impl_output = await workflow.execute_activity(
            "collect_coding_run",
            args=[implementation.get("output_file", ""), implementation.get("host", "")],
            start_to_close_timeout=TIMEOUT_CLAUDE,
            retry_policy=NO_RETRY,
            heartbeat_timeout=timedelta(minutes=2),
        )
        await workflow.execute_activity(
            "comment",
            args=[
                task_id,
                input.agent_id,
                f"Implementation run finished ({impl_output.get('status')}) on branch "
                f"`{implementation.get('branch', '?')}`.",
            ],
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=NO_RETRY,
        )
        if impl_output.get("status") != "succeeded" or not implementation.get("branch"):
            return await self._park_coding(task_id, "implementation did not complete")

        pr_card = await workflow.execute_child_workflow(
            InteractionFlow.run,
            InteractionFlowInput(
                agent_id=input.agent_id,
                kind="choice",
                origin="agent_task_coding_pr",
                prompt=(
                    f"📤 Branch <code>{_esc(implementation['branch'])}</code> is ready in "
                    f"<code>{_esc(repo['github_repo'])}</code>.\n\nOpen a PR?"
                ),
                options={"approve": "✅ Open PR", "skip": "⏭️ Leave the branch"},
                timeout_seconds=172800,
                timeout_policy="archive",
            ),
            id=f"agent-task-pr-{task_id}",
        )
        if (pr_card.response or {}).get("value") != "approve":
            return await self._park_coding(
                task_id, "PR not approved; branch left in place", status="pr_declined"
            )

        # stage_pending_pr takes StagePendingPrInput and returns a PLAIN STRING
        # pending_pr_id (alert_governance.py:103) — not a dict. `alert_fingerprint`
        # is reused as the correlation key; for a task-driven PR that is task:<id>.
        staged = await workflow.execute_activity(
            "stage_pending_pr",
            StagePendingPrInput(
                alert_fingerprint=f"task:{task_id}",
                repo=repo["github_repo"],
                branch=implementation["branch"],
                title=f"{input.task.get('content')}"[:72],
                body=f"Implements Todoist task {task_id}.\n\n{plan[:2000]}",
            ),
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=ACT_RETRY,
        )
        pr = await workflow.execute_activity(
            "create_github_pr",
            CreateGithubPrInput(
                pending_pr_id=staged,
                repo=repo["github_repo"],
                branch=implementation["branch"],
                base="main",
                host=implementation.get("host", ""),
                repo_path=repo["repo_path"],
            ),
            start_to_close_timeout=TIMEOUT_LONG,
            # NO_RETRY, matching the canonical caller (alert_investigation.py:1556):
            # `git push` + `gh pr create` is NOT idempotent. If attempt 1 succeeds
            # but runs past TIMEOUT_LONG, a retried attempt 2's `gh pr create` fails
            # with "a pull request already exists" → status="failed" → this flow
            # would then correctly-but-wrongly report pr_failed while a PR exists.
            retry_policy=NO_RETRY,
        )
        # create_github_pr returns {"pr_url", "status", "error"} and can report
        # status="failed" (push rejected, gh pr create failure, missing
        # pending_pr row) WITHOUT raising — checking pr.get("status") is
        # required, not just reading pr_url, or a failed PR silently reads as
        # opened.
        if pr.get("status") != "opened" or not pr.get("pr_url"):
            return await self._park_coding(
                task_id,
                f"PR creation failed: {pr.get('error') or 'unknown error'}",
                status="pr_failed",
                comment=f"Implementation is on branch `{implementation['branch']}` in "
                f"`{repo['github_repo']}`, but opening the PR failed: "
                f"{pr.get('error') or 'unknown error'}.",
                agent_id=input.agent_id,
            )

        # @waiting, never complete: the PR still needs your review.
        return await self._park_coding(
            task_id,
            "PR opened, awaiting review",
            status="pr_opened",
            comment=f"Opened a PR: {pr['pr_url']}",
            agent_id=input.agent_id,
            repo=repo["github_repo"],
        )
