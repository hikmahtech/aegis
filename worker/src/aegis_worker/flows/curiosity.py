"""CuriosityCardFlow — at most one real question to the owner per day.

A6 built the detector (`CuriosityActivities.find_curiosity_gaps`); this is the
flow that decides whether the top gap is worth a card, asks it, and banks the
answer as durable memory.

Three things make it safe to run unattended:

1. **Budget.** Interaction cards go out via `send_interaction_card`, which
   POSTs straight to `/api/deliver/card` and therefore never passes through
   `safe_send_message` — the notification budget does NOT cover it. So the gate
   is explicit: `check_curiosity_budget` before spawning (own daily cap, the
   shared `should_send` budget, and any still-open curiosity card) and
   `record_curiosity_card` after, so a delivered question consumes the same
   daily budget as a proactive FYI.

2. **Owner exclusion.** A6's calendar detector only excludes the operator's own
   address when the `owner_emails` config key is set, and it is EMPTY in the
   current deployment. Google lists the calendar owner among every event's
   attendees, so with it unset the detector's best candidate can be the owner —
   and the card would ask the owner who they are. Until an operator fills that
   key in, this flow refuses the calendar-attendee lane outright and falls
   through to a finance or project gap (or asks nothing).

3. **One card at a time.** The child is spawned ABANDONED with a deterministic
   id derived from the novelty key, so an overlapping run cannot double-card
   the same subject, and a card blocked on a human for two days never starves
   the schedule (Temporal schedules default to overlap=SKIP).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from temporalio import workflow
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.agent_registry import AgentRegistryActivities
    from aegis_worker.activities.curiosity import CuriosityActivities
    from aegis_worker.flows.interaction import InteractionFlow, InteractionFlowInput
    from aegis_worker.shared.retry import NO_RETRY, TIMEOUT_FAST, TIMEOUT_LLM

# Which behavior tag owns each kind of gap: money questions belong to the
# finance-tagged agent, everything else to the GTD one. Resolution failure or
# an unheld tag falls back to config.agent_id — never a crash, never silence.
_GAP_TAG = {
    "unknown_payee": "finance",
    "calendar_attendee": "gtd",
    "todoist_project": "gtd",
}

# Gap types whose subject cannot be shown to be someone other than the owner
# while `owner_emails` is unconfigured.
_OWNER_SENSITIVE = ("calendar_attendee",)

_ID_SAFE = re.compile(r"[^a-zA-Z0-9_.-]+")


@dataclass
class CuriosityConfig:
    agent_id: str = "sebas"
    max_per_day: int = 1
    limit: int = 5
    # Two days: long enough that a card sent on a busy Friday survives the
    # weekend, short enough that an ignored question archives instead of
    # blocking every later run on the `pending` check.
    timeout_seconds: int = 2 * 86400
    # Threaded from settings by schedule_sync so the Slack card carries the
    # "Open in admin" deep link (cards.py renders no button for `input`
    # without it). Empty = section-only card, still resolvable in the admin.
    aegis_ui_url: str = ""


@workflow.defn(name="CuriosityCardFlow")
class CuriosityCardFlow:
    @workflow.run
    async def run(self, config: CuriosityConfig) -> dict:
        step = "check_curiosity_budget"
        try:
            gate = await workflow.execute_activity_method(
                CuriosityActivities.check_curiosity_budget,
                args=[config.agent_id, config.max_per_day],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
            if not gate.get("allow"):
                return {
                    "status": str(gate.get("reason") or "budget"),
                    "carded": 0,
                    "sent_today": gate.get("sent_today", 0),
                    "pending": gate.get("pending", 0),
                }

            step = "find_curiosity_gaps"
            try:
                candidates = await workflow.execute_activity_method(
                    CuriosityActivities.find_curiosity_gaps,
                    args=[config.agent_id, config.limit],
                    # The detector's optional phrasing pass is an LLM call.
                    start_to_close_timeout=TIMEOUT_LLM,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:  # noqa: BLE001 — a silent day beats a failed run
                workflow.logger.warning("curiosity_gaps_failed err=%s", str(exc)[:200])
                return {"status": "skipped", "carded": 0, "reason": "gap_detection_failed"}

            step = "owner_guard"
            candidates = list(candidates or [])
            if not gate.get("owner_emails_configured"):
                kept = [c for c in candidates if c.get("gap_type") not in _OWNER_SENSITIVE]
                if len(kept) != len(candidates):
                    workflow.logger.warning(
                        "curiosity_owner_lane_refused dropped=%d reason=owner_emails_unset",
                        len(candidates) - len(kept),
                    )
                candidates = kept

            if not candidates:
                return {"status": "no_gaps", "carded": 0}
            top = candidates[0]

            step = "resolve_agents"
            tag = _GAP_TAG.get(str(top.get("gap_type") or ""), "gtd")
            target = config.agent_id
            try:
                resolved = await workflow.execute_activity_method(
                    AgentRegistryActivities.resolve_agents,
                    args=[[tag]],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
                target = (resolved or {}).get(tag) or config.agent_id
            except Exception as exc:  # noqa: BLE001 — routing is a nicety
                workflow.logger.warning("curiosity_agent_resolve_failed err=%s", str(exc)[:200])

            step = "spawn_card"
            novelty_key = str(top.get("novelty_key") or "")
            child_id = f"curiosity-{_ID_SAFE.sub('_', novelty_key)[:180]}"
            options = {"aegis_ui_url": config.aegis_ui_url} if config.aegis_ui_url else None
            metadata = {
                "novelty_key": novelty_key,
                "gap_type": top.get("gap_type"),
                "subject": top.get("subject"),
                "question": top.get("question"),
                "agent_id": target,
            }
            # The money lane reclassifies the payee's backlog by `payee_key`,
            # and the card's metadata is the only place the post-resolve hook
            # can read one from. Without it the answer banks a memory and
            # silently reclassifies nothing.
            payee_key = (top.get("evidence") or {}).get("payee_key")
            if payee_key:
                metadata["payee_key"] = payee_key
            try:
                await workflow.start_child_workflow(
                    InteractionFlow.run,
                    InteractionFlowInput(
                        agent_id=target,
                        # `input` = free-text answer: a deep link in Slack, a
                        # textarea in the admin. Not `choice` — the whole point
                        # is that we do not know the answer's shape.
                        kind="input",
                        origin="curiosity",
                        prompt=str(top.get("question") or ""),
                        options=options,
                        timeout_seconds=config.timeout_seconds,
                        timeout_policy="archive",
                        metadata=metadata,
                        post_resolve_activity="apply_curiosity_answer",
                    ),
                    id=child_id,
                    parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                )
            except WorkflowAlreadyStartedError:
                # The same question is already open — nothing sent, nothing charged.
                return {"status": "duplicate", "carded": 0, "novelty_key": novelty_key}

            step = "record_curiosity_card"
            await workflow.execute_activity_method(
                CuriosityActivities.record_curiosity_card,
                args=[target, True],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
        except Exception as exc:  # noqa: BLE001
            raise ApplicationError(
                f"curiosity_card_failed at step={step}: {exc!r}",
                non_retryable=True,
            ) from exc

        return {
            "status": "sent",
            "carded": 1,
            "agent_id": target,
            "gap_type": top.get("gap_type"),
            "subject": top.get("subject"),
            "novelty_key": novelty_key,
        }
