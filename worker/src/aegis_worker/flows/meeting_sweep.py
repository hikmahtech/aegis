"""MeetingSweepFlow — the notes mail the hourly triage was never shown.

`GmailIngestFlow` fetches `is:unread`, so a note-taker email opened on a phone
before the hourly run is never triaged, never carries the `meeting` tag, and so
never fans out to `MeetingNotesFlow`. That meeting is then lost in silence: no
knowledge row, no observations, no line in the weekly block, and nothing
anywhere that says a meeting went missing.

This flow closes that hole. It looks for the same senders regardless of read
state and regardless of the account cursor, and files whatever has no `meeting`
row yet.

  meeting_sender_addresses          # the `meeting` tag on the user's own
                                    # triage overrides — no vendor in code
    → list_active_channels('email')
    → fetch_emails  from:(...) newer_than:Nd   (no is:unread, no cursor)
    → unstored_meeting_messages     # the ones the hourly path never filed
    → MeetingNotesFlow child per message (ABANDON, id=meeting-notes-<msg id>)

Two deliberate omissions. There is no `is_auth_expired` re-auth card: the
hourly flow already owns that conversation, and a second card for the same
expired token is noise. And a `WorkflowAlreadyStartedError` on the child start
is not an error — it means the hourly path spawned the same child seconds ago,
which is the sweep meeting its own safety net, so it gets its own counter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.gmail import FetchEmailsInput, FetchEmailsResult
    from aegis_worker.flows.meeting_notes import MeetingNotesFlow, MeetingNotesInput
    from aegis_worker.shared.retry import ACT_RETRY, NO_RETRY, TIMEOUT_FAST

_ACT_TIMEOUT = timedelta(seconds=60)
_FETCH_TIMEOUT = timedelta(seconds=300)


def _err_str(exc: BaseException) -> str:
    """The activity's own message, not Temporal's "Activity task failed".

    An ApplicationError raised in the activity arrives wrapped in an
    ActivityError, so recording `str(exc)` would put the same nine useless
    words in every account's `error` field.
    """
    return str(exc.__cause__ or exc)[:200]


@dataclass
class MeetingSweepInput:
    agent_id: str = "sebas"
    lookback_days: int = 7
    max_per_account: int = 50


@workflow.defn(name="MeetingSweepFlow")
class MeetingSweepFlow:
    @workflow.run
    async def run(self, input: MeetingSweepInput) -> dict:
        senders = await workflow.execute_activity(
            "meeting_sender_addresses",
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=ACT_RETRY,
        )
        if not senders:
            # Nobody is tagged `meeting`, so there is nothing this flow could
            # usefully look for. Stop before touching a mailbox — that is what
            # keeps a fresh install inert rather than merely quiet.
            return {"status": "skipped", "reason": "no_meeting_senders"}

        channels = await workflow.execute_activity(
            "list_active_channels",
            "email",
            start_to_close_timeout=_ACT_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        query = f"from:({' OR '.join(senders)}) newer_than:{input.lookback_days}d"

        fetched_total = 0
        filed_total = 0
        already_total = 0
        accounts: list[dict] = []

        for ch in channels:
            label = (ch.get("config") or {}).get("label", ch["identifier"])
            try:
                fetched = await workflow.execute_activity(
                    "fetch_emails",
                    FetchEmailsInput(
                        account_label=label,
                        query=query,
                        # No cursor. Ignoring it is the point: the mail this
                        # flow exists for is older than the last hourly run.
                        since_cursor_ts=None,
                        max_results=input.max_per_account,
                    ),
                    result_type=FetchEmailsResult,
                    start_to_close_timeout=_FETCH_TIMEOUT,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:  # noqa: BLE001 — one dead account, not the run
                reason = _err_str(exc)
                workflow.logger.warning(
                    "meeting_sweep_fetch_failed account=%s err=%s", label, reason
                )
                accounts.append(
                    {"account": label, "fetched": 0, "filed": 0, "error": reason}
                )
                continue

            messages = list(fetched.messages)
            fetched_total += len(messages)
            filed_here = 0
            already_here = 0
            if messages:
                todo = set(
                    await workflow.execute_activity(
                        "unstored_meeting_messages",
                        args=[[str(m.get("id") or "") for m in messages]],
                        start_to_close_timeout=_ACT_TIMEOUT,
                        retry_policy=ACT_RETRY,
                    )
                )
                for msg in messages:
                    if str(msg.get("id") or "") not in todo:
                        continue
                    started = await self._file(input, label, msg)
                    if started == "filed":
                        filed_here += 1
                    elif started == "already_running":
                        already_here += 1

            filed_total += filed_here
            already_total += already_here
            accounts.append(
                {
                    "account": label,
                    "fetched": len(messages),
                    "filed": filed_here,
                    "already_running": already_here,
                }
            )

        return {
            "status": "ok",
            "senders": len(senders),
            "fetched": fetched_total,
            "filed": filed_total,
            "already_running": already_total,
            "accounts": accounts,
        }

    async def _file(self, input: MeetingSweepInput, label: str, msg: dict) -> str:
        """Start one MeetingNotesFlow child. Returns filed / already_running /
        failed — never raises, so one bad message cannot end the sweep."""
        msg_id = str(msg.get("id") or "")
        try:
            await workflow.start_child_workflow(
                MeetingNotesFlow.run,
                MeetingNotesInput(
                    agent_id=input.agent_id,
                    msg=msg,
                    account_label=label,
                ),
                # Same id the hourly fan-out uses, deliberately: it is what
                # makes a double-spawn collide instead of filing twice.
                id=f"meeting-notes-{msg_id}",
                parent_close_policy=ParentClosePolicy.ABANDON,
            )
        except WorkflowAlreadyStartedError:
            # The hourly path got there first and its child is still running.
            # Expected and harmless — the notes ARE being filed.
            #
            # ponytail: both duplicate guards miss a child that is mid-flight —
            # started by the hourly path but not yet past its ingest, so the id
            # is taken only until it finishes and `unstored_meeting_messages`
            # cannot see a row that does not exist yet. Deliberate, not an
            # oversight: `ingest_content` upserts on a content id derived from a
            # deterministic url (`gdoc://<doc_id>`, `aegis://meeting-review/…`),
            # so a duplicate run overwrites the same two rows. The whole cost is
            # one wasted embed + review; never a duplicate or a corrupt row. The
            # fix for a duplicate is therefore to accept it, never a lock or a
            # claim table.
            workflow.logger.info("meeting_sweep_child_already_running msg=%s", msg_id)
            return "already_running"
        except Exception as exc:  # noqa: BLE001 — one message, not the run
            workflow.logger.warning(
                "meeting_sweep_child_start_failed msg=%s err=%s", msg_id, _err_str(exc)
            )
            return "failed"
        return "filed"
