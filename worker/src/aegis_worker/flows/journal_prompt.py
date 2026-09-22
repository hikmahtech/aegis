"""JournalPromptFlow — ask the user about a day they left empty (vault record spec §3).

Daily, at an evening hour on the user's clock (`journal-prompt-daily`). The
day is the one the nightly daylog logged, the last complete local day
(`daylog_local_day` + `logged_day`). `journal_gap_check` reads that day's
notes from a fresh pull, and only a `gap` sends a card: a vault that is off,
daily notes switched off or a failed pull send nothing, because the flow
never asks when it cannot tell whether the user wrote.

The card is one `input` card (Slack's Answer box, or the admin textarea),
started ABANDONED as `journal-prompt-<day>` with REJECT_DUPLICATE, so one day
never gets two cards, even after the first has closed. It expires after
`timeout_seconds` (22 h) with no reminder. Its post-resolve hook,
`file_journal_answer`, files the answer in the day's note and blanks the
stored copy; the card is `private`, so the answer is never in the run's
result either. InteractionFlow swallows a failed hook, and the weekly
`NotesBackfillFlow` files what it left behind.

The flow belongs to the agent the activities row names; started by hand with
no agent, it resolves the holder of the `gtd` capability, whose journal it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from aegis.errors import error_text, logged_failure
    from aegis.services import journal_prompt as jp
    from aegis.services.notes import JOURNAL_OWNER_TAG
    from aegis.services.notes_write import NOTES_WRITE_TIMEOUT_S

    from aegis_worker.activities.agent_registry import AgentRegistryActivities
    from aegis_worker.activities.daylog import DayLogActivities
    from aegis_worker.flows.daylog import logged_day
    from aegis_worker.flows.interaction import InteractionFlow, InteractionFlowInput
    from aegis_worker.shared.retry import NO_RETRY, RETRY_ONCE, TIMEOUT_FAST

# The check pulls the vault, which is a writer's pull in the worst case.
_CHECK_TIMEOUT = timedelta(seconds=NOTES_WRITE_TIMEOUT_S)


@dataclass
class JournalPromptConfig:
    # The owning agent (the activities row's); empty = the journal's owner.
    agent_id: str = ""
    # Fewer words of the user's own than this in the day's notes is a gap.
    min_words: int = jp.DEFAULT_MIN_WORDS
    # How long the card waits for an answer before it is archived.
    timeout_seconds: int = jp.DEFAULT_TIMEOUT_S
    # The card's text; `{day}` becomes the day's name.
    prompt: str = jp.DEFAULT_PROMPT
    # The label over the text box.
    label: str = jp.DEFAULT_LABEL
    # The admin UI, for the card's "Open in admin" link.
    aegis_ui_url: str = ""


@workflow.defn(name="JournalPromptFlow")
class JournalPromptFlow:
    @workflow.run
    async def run(self, config: JournalPromptConfig) -> dict:
        agent_id = config.agent_id or await self._owner()
        if not agent_id:
            return {"status": "skipped", "reason": "no_owner"}
        try:
            clock = await workflow.execute_activity_method(
                DayLogActivities.daylog_local_day,
                args=[workflow.now().isoformat()],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=RETRY_ONCE,
            )
            day = logged_day(date.fromisoformat(str((clock or {}).get("date") or "")))
        except Exception as exc:  # noqa: BLE001 — no day, no card
            workflow.logger.warning("journal_prompt_clock_failed err=%s", error_text(exc))
            return {"status": "skipped", "reason": "clock_failed"}
        label = day.isoformat()
        try:
            check = await workflow.execute_activity(
                "journal_gap_check",
                args=[label, config.min_words],
                start_to_close_timeout=_CHECK_TIMEOUT,
                retry_policy=RETRY_ONCE,
            )
        except Exception as exc:  # noqa: BLE001 — cannot tell, so do not ask
            workflow.logger.warning(
                "journal_prompt_check_failed day=%s err=%s", label, error_text(exc)
            )
            return {"status": "skipped", "reason": "check_failed", "day": label}
        check = check or {}
        status = str(check.get("status") or "")
        if status != "gap":
            return {
                "status": "no_card", "reason": status, "day": label, "words": check.get("words")
            }
        options = {"label": config.label or jp.DEFAULT_LABEL}
        if config.aegis_ui_url:
            options["aegis_ui_url"] = config.aegis_ui_url
        prompt = (config.prompt or jp.DEFAULT_PROMPT).replace(
            "{day}", str(check.get("day_name") or label)
        )
        try:
            await workflow.start_child_workflow(
                InteractionFlow.run,
                InteractionFlowInput(
                    agent_id=agent_id,
                    kind="input",
                    origin=jp.ORIGIN,
                    prompt=prompt,
                    options=options,
                    timeout_seconds=config.timeout_seconds,
                    timeout_policy="archive",
                    metadata={"day": label, "agent_id": agent_id},
                    post_resolve_activity="file_journal_answer",
                    post_resolve_timeout_seconds=NOTES_WRITE_TIMEOUT_S,
                    private=True,
                ),
                id=f"journal-prompt-{label}",
                parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            return {"status": "duplicate", "day": label}
        return {"status": "sent", "day": label, "words": check.get("words")}

    async def _owner(self) -> str:
        """The holder of the `gtd` capability; "" (and a warning) when nobody
        holds it or the lookup fails, and then no card is sent."""
        with logged_failure("journal_prompt_owner_unresolved", logger=workflow.logger):
            resolved = await workflow.execute_activity_method(
                AgentRegistryActivities.resolve_agents,
                args=[[JOURNAL_OWNER_TAG]],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
            return str((resolved or {}).get(JOURNAL_OWNER_TAG) or "")
        return ""
