"""LLMSpendGuardFlow — rolling-24h LLM token budget → kill switch + alert.

Every tick: compare the last 24h of `llm_calls` tokens against
`settings.llm_governor.daily_token_budget`; over budget ⇒ flip
`settings.llm_kill_switch` (refusing new generation calls fleet-wide) and
post a system event. Back under budget ⇒ clear the switch (only if the
governor set it) and post the recovery.

Ships **active but inert**: `daily_token_budget` defaults to 0 = disabled,
so this is a no-op until a budget is set on the admin Settings page. Same
ship-active-but-inert pattern as the social kill switch.

Scheduled every 15 min — cron `*/15 * * * *` in `config/seed/activities.yaml`.
"""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporalio.exceptions import ApplicationError

    from aegis_worker.activities.delivery import DeliveryActivities
    from aegis_worker.activities.llm_governor import LLMGovernorActivities
    from aegis_worker.shared.retry import ACT_RETRY, NO_RETRY, TIMEOUT_FAST


@dataclass
class LLMSpendGuardConfig:
    # agent_id first — WorkflowRunRecorderInterceptor reads it to populate
    # workflow_runs.agent_id (repo convention: every flow config starts here).
    agent_id: str = "pandoras-actor"


@workflow.defn
class LLMSpendGuardFlow:
    """Checks the LLM token budget; alerts only on a state transition."""

    @workflow.run
    async def run(self, config: LLMSpendGuardConfig) -> dict:
        workflow.logger.info("llm_spend_guard_starting")

        try:
            out = await workflow.execute_activity_method(
                LLMGovernorActivities.check_llm_budget,
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=ACT_RETRY,
            )
        except Exception as exc:
            raise ApplicationError(
                f"llm_spend_guard_failed at step=check_llm_budget: {exc!r}",
                non_retryable=True,
            ) from exc

        # Edge-triggered: the activity reports breached/cleared only on the
        # tick that actually flips the switch, so a sustained breach doesn't
        # re-alert every 15 minutes.
        if out.get("breached") or out.get("cleared"):
            try:
                await workflow.execute_activity_method(
                    DeliveryActivities.send_system_event,
                    args=[out["message"]],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            except Exception:
                # The switch is already set in the DB — that's the part that
                # matters. A failed notification must not fail the flow.
                workflow.logger.warning("llm_spend_guard_alert_failed")

        return out
