"""ReceiptIngestFlow — weekly safety-net receipt scan across all Gmail accounts.

Per-message money hygiene is owned by GmailIngestFlow's tag-based fan-out
(financial/payments → MoneyProcessFlow). This flow exists only as a weekly
safety-net: it re-scans recent receipt-shaped mail and fans out any message
the hourly triage missed to MoneyProcessFlow with idempotent semantics.
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

        return {
            "stored": stored,
            "accounts": accounts_processed,
            "errors": errors,
        }

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
