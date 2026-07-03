"""SocialPublishFlow — a Todoist task is a social post, gated by an approval card.

Every 5 min:
1. find_due_posts — open @publish tasks due within the lookahead.
2. Spawn one InteractionFlow card per task, ABANDONED with a deterministic
   id (social-approve-<task_id>) so overlapping ticks can't double-card, and
   a post_resolve hook (apply_social_approval) that applies the choice:
   approve → enqueue + post + complete; skip → strip the publish label.
   Cards are NOT awaited — Temporal schedules default to overlap=SKIP, so a
   tick blocked on a human for hours would starve every later tick (same
   reason ClarifyFlow spawns abandoned children).
3. drain_social_outbox + complete_posted_tasks — retry net for anything the
   hook attempt left pending/failed.
"""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.social import SocialActivities
    from aegis_worker.flows.interaction import InteractionFlow, InteractionFlowInput
    from aegis_worker.shared.retry import ACT_RETRY, NO_RETRY, TIMEOUT_FAST, TIMEOUT_STANDARD


@dataclass
class SocialPublishConfig:
    agent_id: str
    lookahead_minutes: int = 10
    default_post_hour: int = 9  # date-only tasks post at this local hour
    approval_timeout_seconds: int = 86400  # archive after; the next tick re-cards


def _preview(task: dict) -> str:
    lines = [f"📣 Social post due ({', '.join(task['platforms'])})", "", task["text"]]
    if task["link"]:
        lines += ["", task["link"]]
    lines += ["", "Approve to post now; skip to remove the publish label."]
    return "\n".join(lines)[:600]


@workflow.defn(name="SocialPublishFlow")
class SocialPublishFlow:
    @workflow.run
    async def run(self, config: SocialPublishConfig) -> dict:
        step = "find_due_posts"
        carded = 0
        try:
            due = await workflow.execute_activity_method(
                SocialActivities.find_due_posts,
                args=[config.lookahead_minutes, config.default_post_hour],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )

            step = "spawn_cards"
            for task in due:
                child_id = f"social-approve-{str(task['task_id']).replace('/', '_')}"
                try:
                    await workflow.start_child_workflow(
                        InteractionFlow.run,
                        InteractionFlowInput(
                            agent_id=config.agent_id,
                            # "choice" is one of the closed set of card kinds the
                            # Slack renderer + admin panel know how to draw buttons
                            # for (approval|choice|ack|input|draft_review).
                            kind="choice",
                            origin="social_publish",
                            prompt=_preview(task),
                            options={"approve": "✅ Post", "skip": "⏭️ Skip"},
                            timeout_seconds=config.approval_timeout_seconds,
                            timeout_policy="archive",
                            metadata={
                                "task_id": task["task_id"],
                                "platforms": task["platforms"],
                                "text": task["text"],
                                "link": task["link"],
                            },
                            post_resolve_activity="apply_social_approval",
                        ),
                        id=child_id,
                        parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                    )
                    carded += 1
                except WorkflowAlreadyStartedError:
                    # A previous tick's card for this task is still open.
                    continue
                except Exception as spawn_exc:  # noqa: BLE001
                    workflow.logger.warning(
                        "social_publish_card_spawn_failed task_id=%s err=%s",
                        task["task_id"],
                        str(spawn_exc)[:200],
                    )

            step = "drain_social_outbox"
            drained = await workflow.execute_activity_method(
                SocialActivities.drain_social_outbox,
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=NO_RETRY,
            )

            step = "complete_posted_tasks"
            completed = await workflow.execute_activity_method(
                SocialActivities.complete_posted_tasks,
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001
            raise ApplicationError(
                f"social_publish_failed at step={step}: {exc!r}",
                non_retryable=True,
            ) from exc

        return {
            "due": len(due),
            "carded": carded,
            "posted": drained.get("posted", 0),
            "post_failed": drained.get("failed", 0),
            "completed": completed.get("completed", 0),
        }
