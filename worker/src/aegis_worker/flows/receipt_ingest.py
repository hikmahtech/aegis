"""ReceiptIngestFlow — weekly safety-net receipt scan across all Gmail accounts.

Per-message money hygiene is owned by GmailIngestFlow's tag-based fan-out
(financial/payments → MoneyProcessFlow). This flow exists only as a weekly
safety-net: it re-scans recent receipt-shaped mail and fans out any message
the hourly triage missed to MoneyProcessFlow with idempotent semantics.

It also runs a bounded re-attempt sweep (fix #113) over `receipt_email`
rows that MoneyProcessFlow's fire-and-forget pipeline left permanently
stuck — parse/extract failures that predate the 07-16 smart-tier fix.
MoneyProcessFlow can't be reused for this: it starts from `store_receipt_email`,
which is idempotent on `message_id` and would immediately short-circuit as
"duplicate" for an already-stored row. The sweep instead re-drives the
already-hydrated row directly through `classify_and_extract` +
`upsert_charges`.
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

# Hardcoded receipt-shaped sender filter stays in code (versioned with tests).
# The time window is the only knob exposed through seed config (query_window).
_SENDER_FILTER = (
    "(from:billing@ OR from:receipts@ OR from:no-reply@stripe.com "
    "OR from:*@amazon.com OR from:*@razorpay.com "
    "OR from:*@vercel.com)"
)
_DEFAULT_QUERY_WINDOW = "newer_than:14d"


def _build_query(window: str) -> str:
    return f"{_SENDER_FILTER} {window.strip()}"


@dataclass
class ReceiptIngestInput:
    agent_id: str = "maou"
    max_per_account: int = 50
    query_window: str = _DEFAULT_QUERY_WINDOW
    aegis_ui_url: str = ""
    sweep_limit: int = 20
    sweep_older_than_days: int = 1

    @property
    def query(self) -> str:
        return _build_query(self.query_window)


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
        """Bounded re-attempt for receipt_email rows whose `parsed` result
        is missing/failed (fix #113). Reprocesses each directly through
        classify_and_extract + upsert_charges — a row that fails again
        just leaves `parsed` unset and waits for next week's sweep.

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

                extractions = await workflow.execute_activity(
                    "classify_and_extract",
                    args=[receipts, input.agent_id],
                    start_to_close_timeout=_CLASSIFY_TIMEOUT,
                    retry_policy=ACT_RETRY,
                )
                if not extractions or extractions[0].get("_parse_failed"):
                    # Still failing — leave unparsed for next week's sweep.
                    continue

                await workflow.execute_activity(
                    "upsert_charges",
                    args=[receipts[0]["account"], extractions],
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
