"""MoneyProcessFlow — single-email money hygiene pipeline for maou.

Designed to be spawned from GmailIngestFlow as a fire-and-forget child
workflow when a triage-classified email carries any of the financial tags
({"financial", "payments"}). Also used by the weekly ReceiptIngestFlow
safety-net.

Pipeline per email:

  store_receipt_email(msg, account)      # idempotent on message_id
    → "" means duplicate; exit.
  fetch_message_body(account, id)         # full text for the extractor
    → store_receipt_body(receipt_id, body); "" leaves the snippet in place.
  load_receipts([receipt_id])             # hydrate stored row
  classify_and_extract([receipt], "maou") # 1 LLM call, maou persona
    → is_receipt=False means triage false-positive; mark parsed, exit.
  upsert_charges(account, [ext])          # recurring_charge upsert

Failures here are isolated from the parent triage run — the fan-out
hook in GmailIngestFlow starts this with ParentClosePolicy.ABANDON.
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
        receipt_id = await workflow.execute_activity(
            "store_receipt_email",
            args=[input.msg, input.account_label],
            start_to_close_timeout=_ACT_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        if not receipt_id:
            return {"status": "duplicate", "message_id": input.msg.get("id", "")}

        # Full body for the extractor (spec §2 step 2). "" = fetch failed; the
        # snippet stored by store_receipt_email is the fallback.
        body = await workflow.execute_activity(
            "fetch_message_body",
            args=[input.account_label, input.msg.get("id", "")],
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

        receipts = await workflow.execute_activity(
            "load_receipts",
            [receipt_id],
            start_to_close_timeout=_ACT_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        if not receipts:
            return {"status": "load_failed", "receipt_id": receipt_id}

        try:
            extractions = await workflow.execute_activity(
                "classify_and_extract",
                args=[receipts, input.agent_id],
                start_to_close_timeout=_CLASSIFY_TIMEOUT,
                retry_policy=ACT_RETRY,
            )
        except Exception as exc:
            # ACT_RETRY already gave us up to 3 attempts. Treat persistent
            # failure as transient — DON'T mark parsed so the next pass
            # re-tries. receipt_email row stays in the unparsed state.
            workflow.logger.warning(
                "money_extract_failed receipt_id=%s err=%s",
                receipt_id,
                str(exc)[:200],
            )
            return {"status": "extract_failed", "receipt_id": receipt_id}

        if not extractions:
            return {"status": "extract_failed", "receipt_id": receipt_id}

        ext = extractions[0]
        # Per-item parse failure (LLM batch returned a malformed object for
        # this receipt). Distinct from is_receipt=False — we can't trust
        # what was extracted, so don't upsert. Leave receipt_email unparsed
        # so it can be re-processed next run.
        if ext.get("_parse_failed"):
            workflow.logger.warning(
                "money_parse_failed receipt_id=%s — leaving unparsed",
                receipt_id,
            )
            return {"status": "parse_failed", "receipt_id": receipt_id}

        if not ext.get("is_receipt"):
            # Mark parsed so a re-run doesn't burn another LLM call.
            await workflow.execute_activity(
                "upsert_charges",
                args=[input.account_label, [ext]],
                start_to_close_timeout=_ACT_TIMEOUT,
                retry_policy=ACT_RETRY,
            )
            return {"status": "not_a_receipt", "receipt_id": receipt_id}

        processed = await workflow.execute_activity(
            "upsert_charges",
            args=[input.account_label, [ext]],
            start_to_close_timeout=_ACT_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        # No per-receipt Todoist capture any more (spec §7.4): a successful
        # payment is read in the weekly brief, never filed as a task.
        return {
            "status": "charged",
            "receipt_id": receipt_id,
            "processed": processed,
        }
