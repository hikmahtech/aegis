"""Reference stub — the event catalog as code, for the target ``aegis-sdk`` package.

Non-running reference. See ``docs/architecture/productization.md`` §8.

The event bus is the decoupling spine: cross-capability communication happens here,
never via direct imports. Emitters declare events in ``Capability.emits``; consumers
bind handlers via ``Capability.consumes``. Adding a subscriber must NEVER require
changing the emitter.

SAGAS (CHOREOGRAPHY, NOT ORCHESTRATION)
---------------------------------------
Multi-step cross-capability work is choreographed through events, not a central
conductor. Example — the email→money flow that today is a hardcoded child-workflow
spawn becomes:

    mail:   classify_email           -> emit EMAIL_CLASSIFIED {tags:[...]}
    money:  @on(EMAIL_CLASSIFIED) if "financial" in tags -> MoneyProcessFlow
            ... -> emit CHARGE_UPSERTED
    digests:@on(CHARGE_UPSERTED) -> refresh read-model "money.upcoming_renewals"

Each hop is independently testable and independently deployable. Correlation is via a
``saga_id`` carried in every payload so a run can be traced across plugins without any
plugin knowing the others exist.
"""

from __future__ import annotations

from typing import Protocol


class EventBus(Protocol):
    async def emit(self, event: str, payload: dict) -> None: ...
    def subscribe(self, event: str, handler) -> None: ...  # kernel wires from manifests


# Every payload carries these envelope fields (added by the bus, not the emitter):
#   saga_id: str        — correlation id across a choreographed run
#   emitted_by: str     — source capability name
#   emitted_at: str     — ISO timestamp

# --- Canonical event names (the catalog) ---------------------------------------
# Naming: "<domain>.<past-tense-fact>". Payloads documented in productization.md §8.

EMAIL_CLASSIFIED = "email.classified"   # mail      -> {message_id, account, category, tags[]}
CONTENT_INGESTED = "content.ingested"   # knowledge -> {content_id, source_type, url, tags[]}
TRANSCRIBED = "media.transcribed"       # knowledge -> {content_id, source, text_len}
TASK_CAPTURED = "task.captured"         # gtd       -> {task_id, source_tag, labels[]}
REFERENCE_FILED = "reference.filed"     # gtd       -> {task_id, content_id, status}
CHARGE_UPSERTED = "money.charge_upserted"  # money  -> {charge_id, vendor, next_due_at}
ALERT_RECEIVED = "alert.received"       # kernel ingress -> {source, service, fingerprint, payload}
EXEC_REQUESTED = "exec.requested"       # alerts/infra/assistant -> {target, prompt, agent, mode, callback_ref}
EXEC_COMPLETED = "exec.completed"       # exec      -> {handle, verdict, transcript_ref, repo}
PR_PROPOSED = "pr.proposed"             # alerts/exec -> {repo, branch, title, diff, interaction_id}
DRIFT_DETECTED = "drift.detected"       # infra     -> {service, drift_type, severity}

# Read-model refresh signals — a published projection rebuilds when its inputs fire.
READMODEL_STALE = "readmodel.stale"     # kernel    -> {name, version}
