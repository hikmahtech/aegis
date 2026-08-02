"""ProfileReflectionFlow — one proposed edit to the agent's own `user` doc, weekly.

The persona doc an agent reads before every reply is the one piece of state that
compounds: get it right and every later answer is better, get it wrong and every
later answer is wrong in the same way. So it needs to change — and it is also
the most sensitive write target in this codebase, which is why nothing here
writes anything.

The flow gathers a week of evidence, asks one LLM for a revised document, and
hands that document to the human as a `draft_review` card. The write happens
later, in `apply_profile_reflection`, and only if the human pressed Approve.

Four things make it safe to run unattended:

1. **Nothing lands without a human.** The card is a `draft_review`: the admin
   panel shows the proposed document, lets the owner edit it, and submits
   `{action:"approve", edited_doc}` or `{action:"reject", reason}`. Any response
   that is not an approve is a no-op on the profile (the reject reason is still
   banked as a memory by the resolve route's `record_correction_from_interaction`,
   so saying *why* teaches the agent something).

2. **Budget.** Interaction cards go out via `send_interaction_card`, which POSTs
   straight to `/api/deliver/card` and never passes through `safe_send_message`
   — the notification budget does NOT cover it. So the gate is explicit:
   `check_profile_budget` before spawning (own per-day cap, the shared
   `should_send` budget, and any still-open profile draft) and
   `record_profile_card` after.

3. **Silence over noise.** No evidence, an LLM failure, an unparseable reply, or
   a proposal identical to the current doc all end the run as `skipped` with no
   card. A weekly flow that cards unconditionally trains the owner to ignore it.

4. **One card at a time.** The child is spawned ABANDONED with a deterministic
   id derived from the agent and the ISO week, so an overlapping run cannot
   double-card the same week, and a draft blocked on a human for days never
   starves the schedule (Temporal schedules default to overlap=SKIP).
"""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.profile import ProfileActivities
    from aegis_worker.flows.interaction import InteractionFlow, InteractionFlowInput
    from aegis_worker.shared.retry import NO_RETRY, TIMEOUT_FAST, TIMEOUT_LLM

# How much of the proposal goes into the card text. The full document lives in
# the interaction's metadata (and the admin panel renders it); the Slack card is
# a summary with a "Review & send" deep link.
_PROMPT_MAX = 1200


def _card_text(agent_id: str, rationale: str, changed: list[str], days: int) -> str:
    lines = [
        f"📝 Weekly profile draft for *{agent_id}* (last {days} days)",
        "",
    ]
    if rationale:
        lines += [rationale, ""]
    if changed:
        lines += ["Proposed changes:", *changed[:12]]
    else:
        lines.append("Proposed changes: (see the full document in the admin panel)")
    lines += ["", "Review, edit if needed, then Approve — or Reject with a reason."]
    return "\n".join(lines)[:_PROMPT_MAX]


@dataclass
class ProfileReflectionConfig:
    agent_id: str = "sebas"
    lookback_days: int = 7
    max_per_day: int = 1
    # Seven days: the card must survive until the next weekly run, so an ignored
    # draft archives itself just as the following one is proposed rather than
    # blocking every later run on the `pending` check.
    timeout_seconds: int = 7 * 86400
    # Threaded from settings by schedule_sync so the Slack card carries the
    # "Review & send" deep link (cards.py renders no button for `draft_review`
    # without it). Empty = section-only card, still resolvable in the admin.
    aegis_ui_url: str = ""


@workflow.defn(name="ProfileReflectionFlow")
class ProfileReflectionFlow:
    @workflow.run
    async def run(self, config: ProfileReflectionConfig) -> dict:
        step = "check_profile_budget"
        try:
            gate = await workflow.execute_activity_method(
                ProfileActivities.check_profile_budget,
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

            step = "read_profile_context"
            persona = await workflow.execute_activity_method(
                ProfileActivities.read_profile_context,
                args=[config.agent_id],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
            current_doc = str((persona or {}).get("user") or "")

            step = "gather_profile_evidence"
            try:
                evidence = await workflow.execute_activity_method(
                    ProfileActivities.gather_profile_evidence,
                    args=[config.agent_id, config.lookback_days],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:  # noqa: BLE001 — a quiet week beats a failed run
                workflow.logger.warning("profile_evidence_failed err=%s", str(exc)[:200])
                return {"status": "skipped", "carded": 0, "reason": "evidence_failed"}

            evidence = evidence or {}
            if not int(evidence.get("total") or 0):
                return {"status": "skipped", "carded": 0, "reason": "no_evidence"}

            step = "propose_profile_patch"
            try:
                proposal = await workflow.execute_activity_method(
                    ProfileActivities.propose_profile_patch,
                    args=[config.agent_id, evidence, current_doc],
                    start_to_close_timeout=TIMEOUT_LLM,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:  # noqa: BLE001
                workflow.logger.warning("profile_proposal_failed err=%s", str(exc)[:200])
                return {"status": "skipped", "carded": 0, "reason": "llm_failed"}

            proposal = proposal or {}
            proposed_doc = str(proposal.get("proposed_doc") or "")
            if not proposed_doc.strip():
                return {"status": "skipped", "carded": 0, "reason": "llm_failed"}
            if proposal.get("unchanged"):
                return {"status": "skipped", "carded": 0, "reason": "no_change"}

            step = "spawn_card"
            # ISO week, so a retry or an overlapping run in the same week hits
            # the same workflow id and is refused rather than double-carding.
            iso = workflow.now().isocalendar()
            child_id = f"profile-reflection-{config.agent_id}-{iso[0]}w{iso[1]:02d}"
            options = {"aegis_ui_url": config.aegis_ui_url} if config.aegis_ui_url else None
            changed = [str(c) for c in (proposal.get("changed_lines") or [])]
            try:
                await workflow.start_child_workflow(
                    InteractionFlow.run,
                    InteractionFlowInput(
                        agent_id=config.agent_id,
                        # `draft_review` = "here is a document, edit it or say
                        # no". The admin panel draws the approve/reject panel for
                        # exactly this kind; cards.py renders the deep link.
                        kind="draft_review",
                        origin="profile_reflection",
                        prompt=_card_text(
                            config.agent_id,
                            str(proposal.get("rationale") or ""),
                            changed,
                            int(evidence.get("lookback_days") or config.lookback_days),
                        ),
                        options=options,
                        timeout_seconds=config.timeout_seconds,
                        timeout_policy="archive",
                        metadata={
                            "agent_id": config.agent_id,
                            # Which persona doc the approval patches. `user` is
                            # the only one an automated writer may touch.
                            "kind": "user",
                            "proposed_doc": proposed_doc,
                            "revision_of": proposal.get("revision_of"),
                            "rationale": proposal.get("rationale"),
                            "changed_lines": changed[:40],
                        },
                        post_resolve_activity="apply_profile_reflection",
                    ),
                    id=child_id,
                    parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                )
            except WorkflowAlreadyStartedError:
                # This week's draft is already open — nothing sent, nothing charged.
                return {"status": "duplicate", "carded": 0, "child_id": child_id}

            step = "record_profile_card"
            await workflow.execute_activity_method(
                ProfileActivities.record_profile_card,
                args=[config.agent_id, True],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
        except Exception as exc:  # noqa: BLE001
            raise ApplicationError(
                f"profile_reflection_failed at step={step}: {exc!r}",
                non_retryable=True,
            ) from exc

        return {
            "status": "carded",
            "carded": 1,
            "agent_id": config.agent_id,
            "child_id": child_id,
            "evidence_total": int(evidence.get("total") or 0),
            "changed_lines": len(changed),
            "proposed_chars": len(proposed_doc),
        }
