"""DailyReviewFlow + WeeklyReviewFlow — Phase 5 GTD reviews.

Each tick: gather counts → format channel-safe body → send → spawn an
InteractionFlow child (ABANDONED) for acknowledgement → log digest.

See docs/superpowers/specs/2026-05-20-gtd-todoist-phase5-reviews-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from aegis.errors import error_text, logged_failure
    from aegis.services.notes_write import NOTES_WRITE_TIMEOUT_S
    from aegis.services.vault_layout import week_bounds

    from aegis_worker.activities.daylog import DayLogActivities
    from aegis_worker.activities.delivery import DeliveryActivities
    from aegis_worker.activities.review import (
        ReviewActivities,
        format_daily_preview,
        format_key_dates,
        format_meeting_week,
        format_today_focus,
        format_weekly_preview,
    )
    from aegis_worker.flows.interaction import InteractionFlow, InteractionFlowInput
    from aegis_worker.shared.retry import NO_RETRY, RETRY_ONCE, TIMEOUT_FAST, TIMEOUT_LLM

_JOURNAL_TIMEOUT = timedelta(seconds=NOTES_WRITE_TIMEOUT_S)

# Live patch. The weekly review takes minutes and runs once a week, so a
# deploy can land mid-run; a run already in flight has none of the vault step
# in its history, so `patched` answers False on its replay and it finishes as
# recorded, reporting `skipped`. The next Sunday takes the step.
PATCH_FILE_IN_VAULT = "weekly-review-file-in-vault"


@dataclass
class DailyReviewConfig:
    agent_id: str = "sebas"


@dataclass
class WeeklyReviewConfig:
    agent_id: str = "sebas"


async def _spawn_review_interaction(
    agent_id: str,
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
                agent_id=agent_id,
                kind="choice",
                origin=f"gtd_{kind}_review",
                # Cap the prompt — chat channels have their own length limits, and the
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
            error_text(exc),
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
            step = "send_message"
            with logged_failure("daily_review_delivery_failed", logger=workflow.logger):
                await workflow.execute_activity_method(
                    DeliveryActivities.send_message,
                    args=[config.agent_id, preview],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            step = "today_focus"
            with logged_failure("daily_today_focus_failed", logger=workflow.logger):
                focus = await workflow.execute_activity_method(
                    ReviewActivities.gather_today_focus,
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
                await workflow.execute_activity_method(
                    DeliveryActivities.send_message,
                    args=[config.agent_id, format_today_focus(focus)],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            step = "spawn_review_interaction"
            interaction_id = await _spawn_review_interaction(
                config.agent_id, "daily", preview, workflow.info().workflow_id
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


async def _spawn_decision_card(
    agent_id: str, decision: dict, parent_id: str, idx: int
) -> bool:
    """Spawn one abandoned InteractionFlow decision card. Returns True on
    successful spawn. apply_review_decision applies the tapped choice."""
    child_id = f"gtd-weekly-decision-{parent_id}-{idx}"
    try:
        await workflow.start_child_workflow(
            InteractionFlow.run,
            InteractionFlowInput(
                agent_id=agent_id,
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
            "weekly_decision_spawn_failed idx=%s err=%s", idx, error_text(exc)
        )
        return False


@workflow.defn(name="WeeklyReviewFlow")
class WeeklyReviewFlow:
    async def _file_in_vault(self, agent_id: str, narrative: str) -> str:
        """File the review in the vault's note for the week it covers, and say
        what happened: written / exists / not_configured / disabled / error.

        Best-effort by design — the user already has the review in Slack, so a
        vault problem is reported, never raised. The week is the one the run's
        LOCAL date sits in (the daylog's own clock and week-rule activities), so
        the block lands in the same note the daylog's weekly rollup writes."""
        try:
            clock = await workflow.execute_activity_method(
                DayLogActivities.daylog_local_day,
                args=[workflow.now().isoformat()],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=RETRY_ONCE,
            )
            rule = await workflow.execute_activity_method(
                DayLogActivities.vault_week_rule,
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=RETRY_ONCE,
            )
            start, _end, label = week_bounds(
                date.fromisoformat(str((clock or {}).get("date") or "")),
                str((rule or {}).get("week_start") or "monday"),
                str((rule or {}).get("week_numbering") or "iso"),
            )
            res = await workflow.execute_activity(
                "notes_journal_write",
                {
                    "kind": "weekly",
                    "day": start.isoformat(),
                    "label": label,
                    "text": narrative,
                    "agent_id": agent_id,
                    "slot": "review",
                },
                start_to_close_timeout=_JOURNAL_TIMEOUT,
                retry_policy=RETRY_ONCE,
            )
            return str((res or {}).get("status") or "error")
        except Exception as exc:  # noqa: BLE001 — the review is already sent
            workflow.logger.warning("weekly_review_vault_failed err=%s", error_text(exc))
            return "error"

    @workflow.run
    async def run(self, config: WeeklyReviewConfig) -> dict:
        workflow.logger.info("weekly_review_flow_starting")
        step = "gather_weekly_state"
        spawned = 0
        # `skipped` is what a run in flight across the deploy reports: the step
        # is not in its history, so it never runs (`PATCH_FILE_IN_VAULT`).
        vault = "skipped"
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
            # People radar (life.people.key_dates). Appended to the narrative
            # rather than folded into format_weekly_preview, because that
            # formatter is only frame_review's FALLBACK — in production the
            # delivered narrative is the LLM's, so a block added there alone
            # would never ship. Best-effort: an empty/unreachable registry
            # must not cost the user their weekly review.
            step = "check_upcoming_key_dates"
            try:
                key_dates = await workflow.execute_activity_method(
                    ReviewActivities.check_upcoming_key_dates,
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:  # noqa: BLE001
                workflow.logger.warning(
                    "weekly_key_dates_failed err=%s", error_text(exc)
                )
                key_dates = []
            # The formatter is guarded too, and separately: the gather and the
            # render are two different causes and the log has to say which.
            try:
                key_dates_block = format_key_dates(key_dates)
            except Exception as exc:  # noqa: BLE001
                workflow.logger.warning(
                    "weekly_key_dates_format_failed err=%s", error_text(exc)
                )
                key_dates_block = ""
            if key_dates_block:
                narrative = f"{narrative}\n\n{key_dates_block}"
            # Meetings block (MeetingNotesFlow's weekly digest). Same
            # best-effort contract as key dates: a broken meeting query must
            # never cost the user their weekly review.
            step = "gather_meeting_week"
            try:
                meeting_week = await workflow.execute_activity_method(
                    ReviewActivities.gather_meeting_week,
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:  # noqa: BLE001
                workflow.logger.warning("weekly_meeting_week_failed err=%s", error_text(exc))
                meeting_week = {}
            try:
                meeting_block = format_meeting_week(meeting_week)
            except Exception as exc:  # noqa: BLE001
                workflow.logger.warning(
                    "weekly_meeting_week_format_failed err=%s", error_text(exc)
                )
                meeting_block = ""
            if meeting_block:
                narrative = f"{narrative}\n\n{meeting_block}"
            step = "send_message"
            with logged_failure("weekly_review_delivery_failed", logger=workflow.logger):
                await workflow.execute_activity_method(
                    DeliveryActivities.send_message,
                    args=[config.agent_id, narrative],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            step = "file_review_in_vault"
            if workflow.patched(PATCH_FILE_IN_VAULT):
                vault = await self._file_in_vault(config.agent_id, narrative)
            step = "spawn_decisions"
            for i, decision in enumerate(decisions):
                if await _spawn_decision_card(
                    config.agent_id, decision, workflow.info().workflow_id, i
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
        return {
            "kind": "weekly",
            "counts": snapshot,
            "decisions": spawned,
            "vault": vault,
        }
