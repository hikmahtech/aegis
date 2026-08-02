"""SocialPublishFlow — a Todoist task is a social post, gated by an approval card.

Every 5 min:
0. sync_postiz_channels — refresh the `social_accounts` mirror from Postiz
   (throttled to ~hourly by the mirror's own watermark). Step 1 resolves a
   platform LABEL against that mirror, so before #182 a channel connected in
   Postiz stayed unpublishable until a human clicked a button on the admin
   Flows page. Best-effort: a Postiz outage must not stop steps 1-4.
1. retire_unpublishable_tasks — an open @publish task whose every labeled
   platform has no connected account can never publish; comment once and strip
   the publish label so it has an ending instead of re-entering this loop
   forever (#183). Runs BEFORE find_due_posts so the same tick doesn't card it.
2. find_due_posts — open @publish tasks due within the lookahead, plus
   fully-Postiz-routed tasks however far out (Postiz holds the schedule, #60).
3. Spawn one InteractionFlow card per task, ABANDONED with a deterministic
   id (social-approve-<task_id>) so overlapping ticks can't double-card, and
   a post_resolve hook (apply_social_approval) that applies the choice:
   approve → enqueue + post + complete; skip → strip the publish label.
   Cards are NOT awaited — Temporal schedules default to overlap=SKIP, so a
   tick blocked on a human for hours would starve every later tick (same
   reason ClarifyFlow spawns abandoned children).
4. drain_social_outbox + complete_posted_tasks — retry net for anything the
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
    #: Minimum age of the `social_accounts` mirror before it is re-synced from
    #: Postiz. The flow runs every 5 min; 60 keeps the channel list ≤1h stale
    #: for ~24 Postiz calls a day. 0 syncs on every tick.
    channel_sync_minutes: int = 60
    #: Per-tick cap on tasks retired for having no publishable platform.
    max_retire: int = 20


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
        # Best-effort, OUTSIDE the fail-the-flow try below: the mirror refresh
        # and the retirement sweep are both housekeeping. Publishing an
        # already-approved post must not stop because Postiz is unreachable or
        # the Todoist projection is mid-sync.
        try:
            channels = await workflow.execute_activity_method(
                SocialActivities.sync_postiz_channels,
                args=[config.channel_sync_minutes],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=NO_RETRY,
            )
        except Exception:  # noqa: BLE001 — degrade, never take the tick down
            channels = {"status": "sync_failed", "synced": 0}
        try:
            retired = await workflow.execute_activity_method(
                SocialActivities.retire_unpublishable_tasks,
                args=[config.max_retire],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
        except Exception:  # noqa: BLE001
            retired = {"retired": 0}

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
                                "post_at": task.get("post_at", ""),
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

        # NOTE: happy-path posts happen in the approval card's post_resolve
        # (InteractionFlow), not here — drain_social_outbox is only the retry
        # net, so its count is ~always 0 while real posts still flow. The keys
        # say so explicitly; `completed` reflects tasks closed for posted rows.
        return {
            "due": len(due),
            "carded": carded,
            "drain_posted": drained.get("posted", 0),
            "drain_failed": drained.get("failed", 0),
            "completed": completed.get("completed", 0),
            "channel_sync": channels.get("status", "unknown"),
            "channels_synced": channels.get("synced", 0),
            "retired": retired.get("retired", 0),
        }
