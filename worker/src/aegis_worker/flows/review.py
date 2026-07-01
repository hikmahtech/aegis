"""DailyReviewFlow + WeeklyReviewFlow — Phase 5 GTD reviews.

Each tick: gather counts → format Telegram-safe body → send → spawn an
InteractionFlow child (ABANDONED) for acknowledgement → log digest.

See docs/superpowers/specs/2026-05-20-gtd-todoist-phase5-reviews-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.delivery import DeliveryActivities
    from aegis_worker.activities.review import (
        ReviewActivities,
        format_daily_preview,
        format_today_focus,
        format_weekly_preview,
    )
    from aegis_worker.flows.interaction import InteractionFlow, InteractionFlowInput
    from aegis_worker.shared.retry import NO_RETRY, TIMEOUT_FAST, TIMEOUT_LLM


@dataclass
class DailyReviewConfig:
    agent_id: str = "sebas"


@dataclass
class WeeklyReviewConfig:
    agent_id: str = "sebas"


async def _spawn_review_interaction(
    kind: str,
    preview: str,
    parent_id: str,
) -> str | None:
    """Spawn an abandoned InteractionFlow child; returns its workflow_id
    (used as interaction_id placeholder for the audit row) or None on
    spawn failure."""
    safe_kind = kind.replace("/", "_")
    child_id = f"gtd-review-{safe_kind}-{parent_id}"
    options = (
        {"reviewed": "✅ Reviewed", "need_time": "⏰ Need time"}
        if kind == "daily"
        else {"reviewed": "✅ Reviewed weekly", "need_time": "⏰ Defer"}
    )
    timeout_seconds = 43200 if kind == "daily" else 86400  # 12h / 24h
    try:
        await workflow.start_child_workflow(
            InteractionFlow.run,
            InteractionFlowInput(
                agent_id="sebas",
                kind="choice",
                origin=f"gtd_{kind}_review",
                # Cap the prompt — Telegram has its own length limit, and the
                # full preview already went out as the main message.
                prompt=preview[:600],
                options=options,
                metadata={"source": "gtd_review", "kind": kind},
                post_resolve_activity="apply_review_acknowledgement",
                timeout_seconds=timeout_seconds,
                timeout_policy="archive",
            ),
            id=child_id,
            parent_close_policy=workflow.ParentClosePolicy.ABANDON,
        )
        return child_id
    except Exception as exc:  # noqa: BLE001
        workflow.logger.warning(
            "review_interaction_spawn_failed kind=%s err=%s",
            kind,
            str(exc)[:200],
        )
        return None


@workflow.defn(name="DailyReviewFlow")
class DailyReviewFlow:
    @workflow.run
    async def run(self, config: DailyReviewConfig) -> dict:
        workflow.logger.info("daily_review_flow_starting")
        step = "gather_daily_digest"
        try:
            digest = await workflow.execute_activity_method(
                ReviewActivities.gather_daily_digest,
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
            preview = format_daily_preview(digest)
            step = "send_telegram"
            try:
                await workflow.execute_activity_method(
                    DeliveryActivities.send_telegram,
                    args=["sebas", preview],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:  # noqa: BLE001
                workflow.logger.warning(
                    "daily_review_telegram_failed err=%s", str(exc)[:200]
                )
            step = "today_focus"
            try:
                focus = await workflow.execute_activity_method(
                    ReviewActivities.gather_today_focus,
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
                await workflow.execute_activity_method(
                    DeliveryActivities.send_telegram,
                    args=["sebas", format_today_focus(focus)],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:  # noqa: BLE001
                workflow.logger.warning(
                    "daily_today_focus_failed err=%s", str(exc)[:200]
                )
            step = "spawn_review_interaction"
            interaction_id = await _spawn_review_interaction(
                "daily", preview, workflow.info().workflow_id
            )
            step = "log_review_digest"
            await workflow.execute_activity_method(
                ReviewActivities.log_review_digest,
                args=["daily", digest, preview, interaction_id],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
        except ApplicationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ApplicationError(
                f"daily_review_failed at step={step}: {exc!r}",
                non_retryable=True,
            ) from exc
        return {"kind": "daily", "counts": digest, "interaction_id": interaction_id}


async def _spawn_decision_card(decision: dict, parent_id: str, idx: int) -> bool:
    """Spawn one abandoned InteractionFlow decision card. Returns True on
    successful spawn. apply_review_decision applies the tapped choice."""
    child_id = f"gtd-weekly-decision-{parent_id}-{idx}"
    try:
        await workflow.start_child_workflow(
            InteractionFlow.run,
            InteractionFlowInput(
                agent_id="sebas",
                kind="choice",
                origin="gtd_weekly_decision",
                prompt=str(decision.get("prompt") or "")[:600],
                options=decision.get("options") or {},
                metadata={
                    "signal": decision.get("signal"),
                    "task_id": decision.get("task_id"),
                },
                post_resolve_activity="apply_review_decision",
                timeout_seconds=604800,  # a week; next review supersedes
                timeout_policy="archive",
            ),
            id=child_id,
            parent_close_policy=workflow.ParentClosePolicy.ABANDON,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        workflow.logger.warning(
            "weekly_decision_spawn_failed idx=%s err=%s", idx, str(exc)[:200]
        )
        return False


@workflow.defn(name="WeeklyReviewFlow")
class WeeklyReviewFlow:
    @workflow.run
    async def run(self, config: WeeklyReviewConfig) -> dict:
        workflow.logger.info("weekly_review_flow_starting")
        step = "gather_weekly_state"
        spawned = 0
        try:
            snapshot = await workflow.execute_activity_method(
                ReviewActivities.gather_weekly_state,
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
            step = "frame_review"
            framed = await workflow.execute_activity_method(
                ReviewActivities.frame_review,
                args=[snapshot],
                start_to_close_timeout=TIMEOUT_LLM,
                retry_policy=NO_RETRY,
            )
            narrative = framed.get("narrative") or format_weekly_preview(snapshot)
            decisions = framed.get("decisions") or []
            step = "send_telegram"
            try:
                await workflow.execute_activity_method(
                    DeliveryActivities.send_telegram,
                    args=["sebas", narrative],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:  # noqa: BLE001
                workflow.logger.warning(
                    "weekly_review_telegram_failed err=%s", str(exc)[:200]
                )
            step = "spawn_decisions"
            for i, decision in enumerate(decisions):
                if await _spawn_decision_card(
                    decision, workflow.info().workflow_id, i
                ):
                    spawned += 1
            step = "log_review_digest"
            await workflow.execute_activity_method(
                ReviewActivities.log_review_digest,
                args=["weekly", snapshot, narrative, None],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
        except ApplicationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ApplicationError(
                f"weekly_review_failed at step={step}: {exc!r}",
                non_retryable=True,
            ) from exc
        return {"kind": "weekly", "counts": snapshot, "decisions": spawned}
