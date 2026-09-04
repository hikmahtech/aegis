"""ReceiptIngestFlow — weekly safety-net receipt scan across all Gmail accounts.

Per-message money hygiene is owned by GmailIngestFlow's tag-based fan-out
(financial/payments → MoneyProcessFlow). This flow exists only as a weekly
safety-net: it re-scans recent receipt-shaped mail and fans out any message
the hourly triage missed to MoneyProcessFlow with idempotent semantics.

It also runs a bounded re-attempt sweep (fix #113) over every `receipt_email`
row below `parsed.version` 2 — a parse failure, or any row classified by the
pre-books v1 extractor. That makes the sweep the backfill vehicle too: point
`query_window` at an older window, raise `sweep_limit`, and the backlog drains
into the journal a bounded batch per run.

MoneyProcessFlow can't be reused for this: it starts from `store_receipt_email`,
which is idempotent on `message_id` and would short-circuit an already-v2 row
as "duplicate". The sweep instead re-drives the already-hydrated row down the
same v2 path — `load_receipts` → `fetch_message_body` → `store_receipt_body` →
`parse_money_email` → `capture_due`? → `post_money_event` → `store_money_result`.
The body fetch is best-effort: on failure the sweep parses the stored snippet.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from datetime import timedelta
from html import escape as _esc

from temporalio import workflow
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.gmail import FetchEmailsInput, FetchEmailsResult
    from aegis_worker.flows.interaction import InteractionFlow, InteractionFlowInput
    from aegis_worker.flows.money_process import MoneyProcessFlow, MoneyProcessInput
    from aegis_worker.shared.gmail_auth import is_auth_expired
    from aegis_worker.shared.retry import ACT_RETRY, NO_RETRY


_ACT_TIMEOUT = timedelta(seconds=60)
_FETCH_TIMEOUT = timedelta(seconds=120)
_CLASSIFY_TIMEOUT = timedelta(seconds=120)

# The receipt-shaped sender list: banks and card issuers first (they carry the
# amount and the instrument), then the vendors whose receipts enrich them.
# It is a DEFAULT, not a constant — `sender_filter` overrides it from seed
# config, which is how a targeted backfill narrows or widens the scan.
DEFAULT_SENDER_FILTER = (
    "(from:billing@ OR from:receipts@ OR from:no-reply@stripe.com OR from:invoice+statements "
    "OR from:*@amazon.com OR from:*@razorpay.com OR from:*@vercel.com "
    "OR from:alerts@hdfcbank.bank.in OR from:alerts@axis.bank.in OR from:cc.statements@axis.bank.in "
    "OR from:eforexservices@axis.bank.in OR from:alerts@nkgsb-bank.com "
    "OR from:google-pay-noreply@google.com OR from:payments-noreply@google.com "
    "OR from:googleplay-noreply@google.com OR from:no_reply@email.apple.com "
    "OR from:do_not_reply@email.apple.com OR from:ebill@airtel.com OR from:update@airtel.com "
    "OR from:invoicing@aws.com OR from:no-reply@amazonaws.com OR from:noreply@github.com "
    "OR from:notify.cloudflare.com OR from:donotreply@intechonline.net OR from:no-reply@amazonpay.in)"
)
_DEFAULT_QUERY_WINDOW = "newer_than:14d"


@dataclass
class ReceiptIngestInput:
    agent_id: str = "maou"
    max_per_account: int = 50
    query_window: str = _DEFAULT_QUERY_WINDOW
    aegis_ui_url: str = ""
    sweep_limit: int = 20
    sweep_older_than_days: int = 1
    sender_filter: str = DEFAULT_SENDER_FILTER

    @property
    def query(self) -> str:
        return f"{self.sender_filter} {self.query_window.strip()}"


@workflow.defn(name="ReceiptIngestFlow")
class ReceiptIngestFlow:
    @workflow.run
    async def run(self, input: ReceiptIngestInput) -> dict:
        channels = await workflow.execute_activity(
            "list_active_channels",
            "email",
            start_to_close_timeout=_ACT_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        stored = 0
        accounts_processed = 0
        errors = 0

        for ch in channels:
            identifier = ch["identifier"]
            label = (ch.get("config") or {}).get("label", identifier)
            since = (ch.get("config") or {}).get("receipt_last_cursor_ts")

            fetched = await self._fetch_with_reauth(input, label, since)
            if fetched is None:
                errors += 1
                continue

            accounts_processed += 1
            for msg in fetched.messages:
                new = await workflow.execute_activity(
                    "ingest_idempotency_claim",
                    args=["receipt", msg["id"]],
                    start_to_close_timeout=_ACT_TIMEOUT,
                    retry_policy=ACT_RETRY,
                )
                if not new:
                    continue

                stored += 1
                try:
                    await workflow.start_child_workflow(
                        MoneyProcessFlow.run,
                        MoneyProcessInput(
                            agent_id=input.agent_id,
                            msg=msg,
                            account_label=label,
                        ),
                        id=f"money-process-safety-{msg['id']}",
                        parent_close_policy=ParentClosePolicy.ABANDON,
                    )
                except Exception as exc:
                    workflow.logger.warning(
                        "receipt_safety_fanout_failed msg=%s err=%s",
                        msg.get("id", ""),
                        str(exc)[:200],
                    )

            if fetched.latest_internal_date_ms > 0:
                latest_iso = _dt.datetime.fromtimestamp(
                    fetched.latest_internal_date_ms / 1000,
                    tz=_dt.UTC,
                ).isoformat()
                await workflow.execute_activity(
                    "update_channel_config_key",
                    args=["email", identifier, "receipt_last_cursor_ts", latest_iso],
                    start_to_close_timeout=_ACT_TIMEOUT,
                    retry_policy=ACT_RETRY,
                )

        swept = await self._sweep_stuck_receipts(input)

        return {
            "stored": stored,
            "accounts": accounts_processed,
            "errors": errors,
            "swept": swept,
        }

    async def _sweep_stuck_receipts(self, input: ReceiptIngestInput) -> int:
        """Bounded re-attempt for receipt_email rows below `parsed.version`
        2 — a failed parse, or a row the pre-books v1 extractor classified
        (fix #113, widened by the books rework). Each is re-driven down the
        same path MoneyProcessFlow uses: load_receipts → fetch_message_body
        → store_receipt_body → parse_money_email → capture_due? →
        post_money_event → store_money_result. A row that fails again stays
        below version 2 and waits for next week's sweep. The body fetch is
        best-effort; a failure falls back to the stored snippet. This is
        also the backfill path — `sweep_limit` is the batch size.

        # ponytail: no per-row retry-count/backoff bookkeeping — the
        # weekly cadence + a small limit is the whole throttle. Good
        # enough for a bounded, known-small backlog (36 rows); add real
        # tracking only if a genuinely unparseable row starts burning a
        # sweep slot every single week forever.
        """
        stuck_ids = await workflow.execute_activity(
            "find_stuck_receipts",
            args=[input.sweep_limit, input.sweep_older_than_days],
            start_to_close_timeout=_ACT_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        swept = 0
        for receipt_id in stuck_ids:
            try:
                receipts = await workflow.execute_activity(
                    "load_receipts",
                    [receipt_id],
                    start_to_close_timeout=_ACT_TIMEOUT,
                    retry_policy=ACT_RETRY,
                )
                if not receipts:
                    continue

                # The body is an enhancement. A hard failure must not send
                # this row back to next week's sweep — fall through and
                # classify the stored snippet instead.
                try:
                    body = await workflow.execute_activity(
                        "fetch_message_body",
                        args=[receipts[0]["account"], receipts[0]["message_id"]],
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
                        receipts[0]["body_plain"] = body
                except Exception as exc:
                    workflow.logger.warning(
                        "receipt_sweep_body_failed receipt_id=%s err=%s",
                        receipt_id,
                        str(exc)[:200],
                    )

                event = await workflow.execute_activity(
                    "parse_money_email",
                    args=[receipts[0]],
                    start_to_close_timeout=_CLASSIFY_TIMEOUT,
                    retry_policy=ACT_RETRY,
                )
                if not event or event.get("_parse_failed"):
                    # Still failing — leave it below version 2 for next week.
                    continue

                todoist_ref = None
                if event.get("kind") in ("due", "failed"):
                    try:
                        todoist_ref = await workflow.execute_activity(
                            "capture_due",
                            args=[event, receipts[0]["account"], receipts[0]["message_id"]],
                            start_to_close_timeout=_ACT_TIMEOUT,
                            retry_policy=ACT_RETRY,
                        )
                    except Exception as exc:
                        workflow.logger.warning(
                            "receipt_sweep_capture_failed receipt_id=%s err=%s",
                            receipt_id,
                            str(exc)[:200],
                        )

                posted = await workflow.execute_activity(
                    "post_money_event",
                    args=[
                        receipt_id,
                        receipts[0]["account"],
                        receipts[0]["message_id"],
                        event,
                        todoist_ref,
                    ],
                    start_to_close_timeout=_CLASSIFY_TIMEOUT,
                    retry_policy=ACT_RETRY,
                )
                await workflow.execute_activity(
                    "store_money_result",
                    args=[receipt_id, event, posted.get("journal_file")],
                    start_to_close_timeout=_ACT_TIMEOUT,
                    retry_policy=ACT_RETRY,
                )
                swept += 1
            except Exception as exc:
                workflow.logger.warning(
                    "receipt_sweep_failed receipt_id=%s err=%s",
                    receipt_id,
                    str(exc)[:200],
                )
        return swept

    async def _fetch_with_reauth(
        self, input: ReceiptIngestInput, label: str, since: str | None
    ) -> FetchEmailsResult | None:
        """Fetch emails; on auth expired spawn InteractionFlow and retry once."""
        try:
            return await workflow.execute_activity(
                "fetch_emails",
                FetchEmailsInput(
                    account_label=label,
                    query=input.query,
                    since_cursor_ts=since,
                    max_results=input.max_per_account,
                ),
                result_type=FetchEmailsResult,
                start_to_close_timeout=_FETCH_TIMEOUT,
                retry_policy=NO_RETRY,
            )
        except Exception as exc:
            if not is_auth_expired(exc):
                raise

            workflow.logger.warning(
                "receipt_gmail_auth_expired label=%s — pausing for reauth", label
            )
            base = input.aegis_ui_url.rstrip("/")
            url_template = (
                f"{base}/api/admin/gmail/reauth/{label}/initiate?interaction_id={{interaction_id}}"
            )
            result = await workflow.execute_child_workflow(
                InteractionFlow.run,
                InteractionFlowInput(
                    agent_id=input.agent_id,
                    kind="ack",
                    origin="gmail_reauth",
                    prompt=(
                        f"Gmail auth expired for <b>{_esc(label)}</b> (receipt scan). "
                        "Tap below to reauth."
                    ),
                    options={"url": url_template, "button_label": "🔐 Reauth Gmail"},
                    timeout_seconds=86400,
                    timeout_policy="hold",
                ),
                id=f"receipt-reauth-{label}-{workflow.info().workflow_id}",
            )
            if result.status != "resolved":
                return None

            try:
                return await workflow.execute_activity(
                    "fetch_emails",
                    FetchEmailsInput(
                        account_label=label,
                        query=input.query,
                        since_cursor_ts=since,
                        max_results=input.max_per_account,
                    ),
                    result_type=FetchEmailsResult,
                    start_to_close_timeout=_FETCH_TIMEOUT,
                    retry_policy=NO_RETRY,
                )
            except Exception as retry_exc:
                workflow.logger.warning(
                    "receipt_fetch_retry_failed label=%s err=%s",
                    label,
                    str(retry_exc)[:200],
                )
                return None
