"""MoneyProcessFlow — one email, one MoneyEvent, into the books (spec §2).

Spawned by GmailIngestFlow as a fire-and-forget child workflow when a
triage-classified email carries any of the financial tags ({"financial",
"payments"}), and by the weekly ReceiptIngestFlow safety-net for anything
triage missed.

Pipeline per email:

  store_receipt_email(msg, account)   # idempotent on message_id
    → "" means an already-v2 row; exit as a duplicate. A pre-v2 row comes
      back by id so the v1 backlog re-drives through the books pipeline.
  fetch_message_body(account, id)     # full text beats the 200-char snippet
    → store_receipt_body(receipt_id, body); "" or a hard failure leaves the
      snippet in place and the run continues.
  load_receipts([receipt_id])         # hydrate the stored row
  parse_money_email(receipt)          # deterministic parsers, else 1 LLM call
    → `_parse_failed` leaves the row unstamped for the weekly sweep.
  capture_due(event, mailbox, msgid)  # only kind in ("due", "failed")
    → one dated Todoist task; a failure here is logged, never fatal.
  post_money_event(...)               # journal post / link, or index only
  store_money_result(...)             # stamp parsed.version = 2

Failures here are isolated from the parent triage run — the fan-out hook in
GmailIngestFlow starts this with ParentClosePolicy.ABANDON.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.shared.retry import ACT_RETRY

_ACT_TIMEOUT = timedelta(seconds=60)
_CLASSIFY_TIMEOUT = timedelta(seconds=120)


@dataclass
class MoneyProcessInput:
    agent_id: str
    msg: dict
    account_label: str


@workflow.defn(name="MoneyProcessFlow")
class MoneyProcessFlow:
    @workflow.run
    async def run(self, input: MoneyProcessInput) -> dict:
        msg_id = input.msg.get("id", "")
        receipt_id = await workflow.execute_activity(
            "store_receipt_email",
            args=[input.msg, input.account_label],
            start_to_close_timeout=_ACT_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        if not receipt_id:
            return {"status": "duplicate", "message_id": msg_id}
        out = {"receipt_id": receipt_id, "msgid": f"{input.account_label}/{msg_id}", "kind": None}

        # Full body for the parser (spec §2 step 2). "" = fetch soft-failed;
        # the snippet stored by store_receipt_email is the fallback. A HARD
        # failure (timeout, or a raise before the activity's own soft-fail)
        # must not kill the run either: parsing less text is strictly better
        # than losing the whole receipt to a Gmail blip.
        try:
            body = await workflow.execute_activity(
                "fetch_message_body",
                args=[input.account_label, msg_id],
                start_to_close_timeout=_ACT_TIMEOUT,
                retry_policy=ACT_RETRY,
            )
            if body:
                await workflow.execute_activity(
                    "store_receipt_body",
                    args=[receipt_id, body],
                    start_to_close_timeout=_ACT_TIMEOUT,
                    retry_policy=ACT_RETRY,
                )
        except Exception as exc:
            workflow.logger.warning(
                "money_body_fetch_failed receipt_id=%s err=%s",
                receipt_id,
                str(exc)[:200],
            )

        receipts = await workflow.execute_activity(
            "load_receipts",
            [receipt_id],
            start_to_close_timeout=_ACT_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        if not receipts:
            return {**out, "status": "load_failed"}

        try:
            event = await workflow.execute_activity(
                "parse_money_email",
                args=[receipts[0]],
                start_to_close_timeout=_CLASSIFY_TIMEOUT,
                retry_policy=ACT_RETRY,
            )
        except Exception as exc:
            # Persistent parser/LLM failure: leave the row below version 2 so
            # the weekly sweep re-drives it.
            workflow.logger.warning(
                "money_extract_failed receipt_id=%s err=%s",
                receipt_id,
                str(exc)[:200],
            )
            return {**out, "status": "extract_failed"}

        if not event or event.get("_parse_failed"):
            workflow.logger.warning(
                "money_parse_failed receipt_id=%s — leaving unstamped",
                receipt_id,
            )
            return {**out, "status": "parse_failed"}

        kind = event.get("kind", "ignore")
        out["kind"] = kind

        todoist_ref = None
        if kind in ("due", "failed"):
            # A bill or a failed payment is the only thing that becomes a task
            # (spec §7.1). Todoist being down must not stop the event reaching
            # the index — the task is the reminder, the index is the record.
            try:
                todoist_ref = await workflow.execute_activity(
                    "capture_due",
                    args=[event, input.account_label, msg_id],
                    start_to_close_timeout=_ACT_TIMEOUT,
                    retry_policy=ACT_RETRY,
                )
            except Exception as exc:
                workflow.logger.warning(
                    "money_capture_due_failed receipt_id=%s err=%s",
                    receipt_id,
                    str(exc)[:200],
                )

        posted = await workflow.execute_activity(
            "post_money_event",
            args=[receipt_id, input.account_label, msg_id, event, todoist_ref],
            start_to_close_timeout=_CLASSIFY_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        await workflow.execute_activity(
            "store_money_result",
            args=[receipt_id, event, posted.get("journal_file")],
            start_to_close_timeout=_ACT_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        status = "ignored" if kind == "ignore" else posted.get("status", "indexed")
        return {**out, "status": status}
