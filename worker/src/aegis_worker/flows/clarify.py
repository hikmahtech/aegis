"""ClarifyFlow — Phase 3 Inbox clarification, scheduled every 15 min.

Per-tick: pull unclassified Inbox items, classify each, apply outcome
(or NEEDS REVIEW note when low-confidence), log the decision. Errors
on a single item swallow and continue so a poisonous task can't break
the whole tick.

## Classifications (9 total)

Five base GTD outcomes from `classify_one`:

- ``trash``       — label `#trash` + complete
- ``reference``   — add `@reference` label (state-as-label, no item_move)
- ``someday``     — add `@someday` label (state-as-label, no item_move),
                  mirroring `reference`/`@reference`
- ``2_min``       — in-window: spawn chat card (Do now / Defer / Trash);
                  out-of-window: add `@5min` label
- ``next_action`` — label update (optional `due.string="tomorrow"` from defer).
                  Multi-step work uses Todoist subtasks, not sub-projects.

Three pandora short-circuits (added 2026-05-20):

- ``pandora_owned``         — task already labelled `@pandora`; no-op apply
- ``pandora_investigation`` — title matches `^APP-\\d+:` Acme Jira key
- ``pandora_followup``      — user comment on existing `@pandora` APP-task
                            triggers a fresh investigation with the comment
                            appended as alert context

## Interaction spawn (Phase 4)

When apply_outcome returns ``interaction_spawned=True``:

- ``spawn_kind != "pandora_investigation"`` (low_conf / 2_min) — start
  `InteractionFlow` as an abandoned child; the child's post_resolve_activity
  (`apply_clarify_resolution`) applies the chosen action when the user
  responds.
- ``spawn_kind == "pandora_investigation"`` — start `AlertInvestigationFlow`
  bound to the existing task via `todoist_task_id`. Skip spawn when
  ``applied=False`` so a stale-projection rejection doesn't double-fire.

## Watermark invariant (added 2026-05-21, refined 2026-05-23)

`log_classification(bump_watermark=…)` controls `todoist_tasks.last_clarified_at`.
Bump only when: `applied=True` OR `outbox_queued > 0` OR (non-pandora
interaction spawned AND apply landed). Otherwise keep watermark NULL so the
task re-enters `find_unclassified_items` once the projection catches up.
Migration 016 was added to repair watermarks poisoned by the original
unconditional bump.
"""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.clarify import ClarifyActivities
    from aegis_worker.flows.agent_chat_reply import AgentChatReplyFlow, AgentChatReplyInput
    from aegis_worker.flows.alert_investigation import AlertInvestigationFlow
    from aegis_worker.flows.interaction import InteractionFlow, InteractionFlowInput
    from aegis_worker.shared.retry import (
        NO_RETRY,
        STANDARD,
        TIMEOUT_FAST,
        TIMEOUT_LLM,
        TIMEOUT_STANDARD,
    )


async def _dispatch_reference_verdict(task: dict, verdict: dict) -> None:
    """Branch on the ingest verdict: complete the task, demote it, or no-op.

    Called from ClarifyFlow.run after ingest_reference_to_ks returns (or
    its retries exhausted). Lives at module level so apply_clarify_resolution's
    inline path can reuse it without duplicating the dispatch logic.

    If Todoist rejects ``complete_reference_task`` permanently we fall through
    to the demotion path so the user sees the failure in chat + a
    @to-read lane in Library — without this fallback the task stays open with
    the @reference label forever (KS has the content; Todoist doesn't).
    Transient (retryable) Todoist failures are surfaced as exceptions so
    Temporal's STANDARD retry policy can retry the activity.
    """
    status = verdict.get("status")
    if status == "ok":
        result = await workflow.execute_activity_method(
            ClarifyActivities.complete_reference_task,
            args=[
                task["id"],
                task.get("content") or "",
                task.get("source_tag") or "",
                verdict.get("content_id"),
                verdict.get("url"),
            ],
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=STANDARD,
        )
        if not result.get("completed"):
            reason = result.get("reason") or "unknown"
            if result.get("retryable"):
                # Re-raise so the activity-level STANDARD policy retries — and
                # if all retries exhaust, the workflow surfaces the failure
                # instead of silently leaving the task half-done.
                raise RuntimeError(f"complete_reference_task transient failure: {reason}")
            # Permanent: fall through to demotion so the user gets a signal.
            workflow.logger.warning(
                "complete_reference_task_permanent_failure task=%s reason=%s",
                task["id"],
                reason,
            )
            await workflow.execute_activity_method(
                ClarifyActivities.reclassify_reference_to_reading,
                args=[
                    task["id"],
                    task.get("content") or "",
                    task.get("source_tag") or "",
                    list(task.get("labels") or []),
                    f"todoist rejected completion: {reason}",
                ],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=NO_RETRY,
            )
    elif status == "permanent_error":
        await workflow.execute_activity_method(
            ClarifyActivities.reclassify_reference_to_reading,
            args=[
                task["id"],
                task.get("content") or "",
                task.get("source_tag") or "",
                list(task.get("labels") or []),
                verdict.get("reason") or "unknown",
            ],
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=NO_RETRY,
        )
    # status == "skipped" or anything else → no-op


@dataclass
class ClarifyConfig:
    agent_id: str = "sebas"
    max_items: int = 20


@workflow.defn(name="ClarifyFlow")
class ClarifyFlow:
    @workflow.run
    async def run(self, config: ClarifyConfig) -> dict:
        workflow.logger.info("clarify_flow_starting")

        tasks = await workflow.execute_activity_method(
            ClarifyActivities.find_unclassified_items,
            args=[config.max_items],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=NO_RETRY,
        )

        applied = 0
        interactions = 0
        for task in tasks:
            try:
                user_hint = task.get("latest_user_note")
                pass_n = 2 if user_hint else 1
                # classify_one calls qwen3:14b (primary) and may escalate to
                # Sonnet on low confidence — both can take 30-60s under load,
                # occasionally longer. Use TIMEOUT_LLM (180s) not TIMEOUT_FAST.
                decision = await workflow.execute_activity_method(
                    ClarifyActivities.classify_one,
                    args=[task],
                    start_to_close_timeout=TIMEOUT_LLM,
                    retry_policy=NO_RETRY,
                )
                # Carry source_tag through for logging
                decision["source_tag"] = task.get("source_tag")
                outcome = await workflow.execute_activity_method(
                    ClarifyActivities.apply_outcome,
                    args=[task, decision, pass_n],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
                # Watermark invariant (2026-05-21, refined 2026-05-23):
                # Only bump last_clarified_at when:
                #   - apply succeeded (terminal), OR
                #   - apply failed BUT outbox compensation was queued
                #     (drain_outbox will retry), OR
                #   - a NON-pandora interaction was spawned (low_conf /
                #     2_min: the chat card is the authoritative state,
                #     so we bump even if the paper-trail note failed).
                #
                # Pandora interactions are different: the spawn is gated on
                # `applied=True` at lines 147-151 below. If applied=False
                # (typically a non-retryable rejection from a stale
                # projection), we don't spawn the investigation — bumping
                # the watermark would lose the user's followup comment.
                # Keep the watermark NULL so the next tick re-classifies
                # once the projection catches up.
                payload = outcome.get("interaction_payload") or {}
                spawn_kind = payload.get("spawn_kind")
                interaction_will_spawn = bool(
                    outcome.get("interaction_spawned")
                    and (spawn_kind != "pandora_investigation" or outcome.get("applied"))
                )
                bump_watermark = bool(
                    outcome.get("applied")
                    or outcome.get("outbox_queued", 0) > 0
                    or interaction_will_spawn
                )
                await workflow.execute_activity_method(
                    ClarifyActivities.log_classification,
                    args=[
                        task["id"],
                        decision,
                        outcome.get("applied", False),
                        pass_n,
                        user_hint,
                        bump_watermark,
                    ],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
                if outcome.get("applied"):
                    applied += 1
                # references-as-knowledge (2026-05-21): when the task
                # classifies as a reference AND the Todoist label-update
                # succeeded, push the body into KS. The activity raises on
                # transient KS failures (5xx, connect, timeout) so
                # Temporal's STANDARD retry policy gives KS time to come
                # back. Permanent failures (4xx, scraper bailed) demote
                # the task to a reading lane; successful ingests complete
                # the Todoist task — KS is the home now.
                if outcome.get("applied") and decision.get("classification") == "reference":
                    verdict: dict = {"status": "skipped"}
                    try:
                        verdict = await workflow.execute_activity_method(
                            ClarifyActivities.ingest_reference_to_ks,
                            args=[
                                task["id"],
                                task.get("content") or "",
                                task.get("description") or "",
                                task.get("source_tag") or "",
                                task.get("latest_user_note"),
                            ],
                            start_to_close_timeout=TIMEOUT_LLM,
                            retry_policy=STANDARD,
                        )
                    except Exception as ingest_exc:  # noqa: BLE001
                        # STANDARD retries exhausted → treat as permanent
                        # and demote so the user still ends up with a
                        # reading task instead of a stuck @reference.
                        workflow.logger.warning(
                            "clarify_flow_ks_ingest_exhausted task_id=%s err=%s",
                            task.get("id"),
                            str(ingest_exc)[:200],
                        )
                        verdict = {
                            "status": "permanent_error",
                            "reason": f"retries_exhausted: {str(ingest_exc)[:120]}",
                        }
                    await _dispatch_reference_verdict(task, verdict)
                # Phase 4: when apply_outcome flagged an interaction, start
                # InteractionFlow as a fire-and-forget abandoned child so it
                # outlives this ClarifyFlow tick (24h timeout). The child's
                # post_resolve_activity will apply the chosen action.
                #
                # Pandora branch (2026-05-20): if the payload's spawn_kind
                # is pandora_investigation, fire AlertInvestigationFlow
                # instead of InteractionFlow — the alert dict carries the
                # existing todoist_task_id so the investigation attaches
                # to the current task rather than creating a new one.
                if outcome.get("interaction_spawned") and outcome.get("interaction_payload"):
                    payload = outcome["interaction_payload"]
                    safe_task_id = str(task["id"]).replace("/", "_")
                    spawn_kind = payload.get("spawn_kind")
                    if spawn_kind == "pandora_investigation":
                        # Only spawn the alert flow when the @pandora label
                        # actually landed on the Todoist task. Spawning on a
                        # non-retryably-failed apply leaves the task without
                        # the @pandora short-circuit label, so the next tick
                        # would re-classify and re-spawn — a double
                        # investigation against a stale projection.
                        if not outcome.get("applied"):
                            workflow.logger.warning(
                                "clarify_flow_pandora_skip_spawn_unapplied task_id=%s",
                                task.get("id"),
                            )
                        else:
                            alert_dict = payload["alert"]
                            child_id = f"pandora-jira-{safe_task_id}-{workflow.info().workflow_id}"
                            try:
                                await workflow.start_child_workflow(
                                    AlertInvestigationFlow.run,
                                    alert_dict,
                                    id=child_id,
                                    parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                                )
                                interactions += 1
                            except Exception as spawn_exc:  # noqa: BLE001
                                workflow.logger.warning(
                                    "clarify_flow_spawn_pandora_failed task_id=%s err=%s",
                                    task.get("id"),
                                    str(spawn_exc)[:200],
                                )
                    elif spawn_kind == "agent_chat_reply":
                        # Per-agent comment-channel spawn (2026-05-26).
                        #
                        # Dispatch AgentChatReplyFlow as an ABANDON child and
                        # do NOT await its result. A child started with
                        # ParentClosePolicy.ABANDON is detached, so
                        # `child_handle.result()` raises "Result is not set."
                        # immediately — which previously tripped the error
                        # compensator on EVERY tick and cleared the watermark,
                        # re-eligibling the task into a 15-min duplicate-reply
                        # loop (fixed 2026-05-30). On a successful spawn the
                        # watermark bump from log_classification stands; the
                        # child posts its own reply, and its own error comment
                        # on permanent failure (agent_chat_reply.py). Only a
                        # spawn-time raise (the child never started) rolls the
                        # watermark back so the user's comment isn't consumed.
                        child_id = f"agent-chat-reply-{safe_task_id}-{workflow.info().workflow_id}"
                        try:
                            await workflow.start_child_workflow(
                                AgentChatReplyFlow.run,
                                AgentChatReplyInput(
                                    target_agent=payload["target_agent"],
                                    task_id=payload["task_id"],
                                    synthetic_user_message=payload["synthetic_input"],
                                    thread_id=payload["thread_id"],
                                ),
                                id=child_id,
                                parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                            )
                            interactions += 1
                        except Exception as spawn_exc:  # noqa: BLE001
                            # Compensating action: the watermark already
                            # bumped in log_classification, so the comment
                            # would be consumed unless we restore it AND
                            # tell the user we dropped the reply.
                            workflow.logger.warning(
                                "clarify_flow_spawn_agent_chat_failed task_id=%s err=%s",
                                task.get("id"),
                                str(spawn_exc)[:200],
                            )
                            try:
                                await workflow.execute_activity_method(
                                    ClarifyActivities.post_agent_reply_error_comment,
                                    args=[
                                        task["id"],
                                        payload.get("target_agent", "unknown"),
                                        f"spawn failed: {str(spawn_exc)[:120]}",
                                    ],
                                    start_to_close_timeout=TIMEOUT_FAST,
                                    retry_policy=NO_RETRY,
                                )
                            except Exception as err_exc:  # noqa: BLE001
                                workflow.logger.warning(
                                    "clarify_flow_spawn_err_comment_failed task_id=%s err=%s",
                                    task.get("id"),
                                    str(err_exc)[:200],
                                )
                            try:
                                await workflow.execute_activity_method(
                                    ClarifyActivities.clear_clarify_watermark,
                                    args=[task["id"]],
                                    start_to_close_timeout=TIMEOUT_FAST,
                                    retry_policy=NO_RETRY,
                                )
                            except Exception as clear_exc:  # noqa: BLE001
                                workflow.logger.warning(
                                    "clarify_flow_spawn_watermark_clear_failed task_id=%s err=%s",
                                    task.get("id"),
                                    str(clear_exc)[:200],
                                )
                    else:
                        child_id = (
                            f"gtd-clarify-{payload['flavor']}-{safe_task_id}"
                            f"-pass{pass_n}-{workflow.info().workflow_id}"
                        )
                        try:
                            await workflow.start_child_workflow(
                                InteractionFlow.run,
                                InteractionFlowInput(
                                    agent_id=config.agent_id,
                                    kind="choice",
                                    origin=f"gtd_clarify_{payload['flavor']}",
                                    prompt=payload["prompt"],
                                    options=payload["options"],
                                    metadata={
                                        "source": "gtd_clarify",
                                        "flavor": payload["flavor"],
                                        "task_id": task["id"],
                                        "decision": payload["decision"],
                                        "pass_n": payload["pass_n"],
                                    },
                                    post_resolve_activity="apply_clarify_resolution",
                                    timeout_seconds=86400,
                                    timeout_policy="archive",
                                ),
                                id=child_id,
                                parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                            )
                            interactions += 1
                        except Exception as spawn_exc:  # noqa: BLE001
                            workflow.logger.warning(
                                "clarify_flow_spawn_interaction_failed task_id=%s err=%s",
                                task.get("id"),
                                str(spawn_exc)[:200],
                            )
            except Exception as exc:  # noqa: BLE001
                workflow.logger.warning(
                    "clarify_flow_item_failed task_id=%s error=%s",
                    task.get("id"),
                    str(exc)[:200],
                )
                continue

        return {
            "found": len(tasks),
            "applied": applied,
            "interactions": interactions,
        }
