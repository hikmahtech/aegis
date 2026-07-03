"""DailyBriefingFlow — morning summary delivered to the agent's channel.

Gathers pending interactions, calendar events, knowledge stats,
intelligence items, and (for Maou) market data, then delivers a
concise briefing to the agent's channel.

Scheduled daily (10:00 IST / 04:30 UTC) — cron `30 4 * * *` in
`config/seed/activities.yaml`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.alerts import AlertActivities
    from aegis_worker.activities.briefing import BriefingActivities
    from aegis_worker.activities.delivery import DeliveryActivities
    from aegis_worker.shared.retry import (
        NO_RETRY,
        RETRY_ONCE,
        TIMEOUT_FAST,
        TIMEOUT_LLM,
    )


@dataclass
class DailyBriefingConfig:
    """Configuration for DailyBriefingFlow.

    NOTE: `chat_id` was dropped (2026-05-28) — the scheduled-path
    builder in `schedule_sync._ACTIVITY_TYPE_MAP["DailyBriefingFlow"]` never
    threaded a chat_id through, so the field always rode at 0 and
    `send_message(chat_id=0)` already resolves to the agent's
    default topic via the bot's routing. Removing the field stops
    pretending it's a useful knob.
    """

    agent_id: str = "sebas"


@workflow.defn
class DailyBriefingFlow:
    """Generate and deliver a daily briefing."""

    @workflow.run
    async def run(self, config: DailyBriefingConfig) -> dict:
        workflow.logger.info("briefing_starting")

        try:
            await workflow.execute_activity_method(
                DeliveryActivities.send_system_event,
                args=[f"⏳ DailyBriefing [{config.agent_id}] started"],
                start_to_close_timeout=TIMEOUT_FAST, retry_policy=NO_RETRY,
            )
        except Exception:
            pass

        changes = await workflow.execute_activity_method(
            BriefingActivities.gather_briefing_changes,
            start_to_close_timeout=TIMEOUT_LLM, retry_policy=NO_RETRY,
        )
        narrative = await workflow.execute_activity_method(
            BriefingActivities.frame_briefing,
            args=[changes],
            start_to_close_timeout=TIMEOUT_LLM, retry_policy=NO_RETRY,
        )

        # maou market appends to the same message (kept behavior)
        if config.agent_id == "maou":
            try:
                market = await workflow.execute_activity_method(
                    BriefingActivities.gather_market_data,
                    start_to_close_timeout=TIMEOUT_FAST, retry_policy=NO_RETRY,
                )
                if market.get("available"):
                    market_section = await workflow.execute_activity_method(
                        BriefingActivities.format_market_section,
                        args=[market],
                        start_to_close_timeout=TIMEOUT_FAST, retry_policy=NO_RETRY,
                    )
                    if market_section:
                        narrative += f"\n\n{market_section}"
            except Exception:
                pass

        msg = f"<b>Daily Briefing</b>\n\n{narrative}"
        sent_ok = False
        try:
            await workflow.execute_activity_method(
                DeliveryActivities.send_message,
                args=[config.agent_id, msg, 0],
                start_to_close_timeout=TIMEOUT_FAST, retry_policy=RETRY_ONCE,
            )
            sent_ok = True
        except Exception:
            workflow.logger.warning("briefing_delivery_failed")

        # Additive per-persona voice note (no-op unless AEGIS_TTS_ENABLED). Reads
        # the plain narrative, not the HTML message. Capped so a long briefing
        # doesn't run up TTS cost. ponytail: 3000-char cap, raise if briefings
        # routinely overflow.
        if sent_ok:
            try:
                await workflow.execute_activity_method(
                    DeliveryActivities.send_voice,
                    args=[config.agent_id, narrative[:3000]],
                    start_to_close_timeout=TIMEOUT_FAST, retry_policy=NO_RETRY,
                )
            except Exception:
                workflow.logger.warning("briefing_voice_failed")

        # pandora alert digest (kept)
        try:
            digest = await workflow.execute_activity_method(
                AlertActivities.build_alert_digest,
                start_to_close_timeout=TIMEOUT_FAST, retry_policy=NO_RETRY,
            )
            if digest.get("count", 0) > 0:
                await workflow.execute_activity_method(
                    DeliveryActivities.send_message,
                    args=["pandoras-actor", digest["message"], 0],
                    start_to_close_timeout=TIMEOUT_FAST, retry_policy=NO_RETRY,
                )
        except Exception:
            pass

        target_date = workflow.now().strftime("%Y-%m-%d")
        try:
            await workflow.execute_activity_method(
                BriefingActivities.ingest_briefing,
                args=[narrative, target_date],
                start_to_close_timeout=timedelta(seconds=60), retry_policy=NO_RETRY,
            )
        except Exception:
            workflow.logger.warning("briefing_ingest_failed")

        # commit the snapshot ONLY after a successful main send (a failed send
        # re-reports next run rather than dropping the changes).
        if sent_ok:
            try:
                await workflow.execute_activity_method(
                    BriefingActivities.commit_briefing_state,
                    args=[changes["_new_state"]],
                    start_to_close_timeout=TIMEOUT_FAST, retry_policy=RETRY_ONCE,
                )
            except Exception:
                workflow.logger.warning("briefing_state_commit_failed")

        try:
            await workflow.execute_activity_method(
                DeliveryActivities.send_system_event,
                args=[f"✅ DailyBriefing [{config.agent_id}] completed"],
                start_to_close_timeout=TIMEOUT_FAST, retry_policy=NO_RETRY,
            )
        except Exception:
            pass

        return {"status": "delivered", "quiet": changes.get("quiet", False)}
