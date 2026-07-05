"""GmailIngestFlow — hourly email triage across all Gmail accounts.

Iterates channels WHERE kind='email' AND active. Per account:
  1. fetch_emails (since last_cursor_ts)
  2. On auth failure → spawn InteractionFlow(kind='ack', timeout_policy='hold')
     → retry after resolve
  3. For each message: classify → route → idempotency claim
  4. Update last_cursor_ts on success
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from datetime import timedelta
from html import escape as _esc

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.agent_registry import AgentRegistryActivities
    from aegis_worker.activities.capture import CaptureActivities
    from aegis_worker.activities.delivery import DeliveryActivities
    from aegis_worker.activities.gmail import (
        FetchEmailsInput,
        FetchEmailsResult,
    )
    from aegis_worker.flows.interaction import InteractionFlow, InteractionFlowInput
    from aegis_worker.flows.money_process import MoneyProcessFlow, MoneyProcessInput
    from aegis_worker.shared.gmail_auth import is_auth_expired
    from aegis_worker.shared.retry import ACT_RETRY, NO_RETRY, TIMEOUT_FAST, TIMEOUT_STANDARD


_ACT_TIMEOUT = timedelta(seconds=60)
_FETCH_TIMEOUT = timedelta(seconds=900)
_CLASSIFY_TIMEOUT = timedelta(seconds=180)
# Pause between LLM classification calls to avoid overwhelming qwen.
_LLM_THROTTLE_SECS = 2


@dataclass
class GmailIngestInput:
    agent_id: str = "sebas"
    max_per_account: int = 0  # 0 = no limit (fetch all via pagination)
    query: str = "is:unread newer_than:30d"
    aegis_ui_url: str = ""


@workflow.defn(name="GmailIngestFlow")
class GmailIngestFlow:
    @workflow.run
    async def run(self, input: GmailIngestInput) -> dict:
        channels = await workflow.execute_activity(
            "list_active_channels",
            "email",
            start_to_close_timeout=_ACT_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        total_processed = 0
        by_category: dict[str, int] = {}
        by_source: dict[str, int] = {}
        per_account: list[dict] = []

        # Financial fan-out belongs to whichever agent holds the `finance`
        # behavior tag (issue #36), resolved once per run — no literal id. If
        # nothing holds it, the MoneyProcessFlow fan-out is skipped.
        try:
            _resolved = await workflow.execute_activity_method(
                AgentRegistryActivities.resolve_agents,
                args=[["finance"]],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
            finance_agent = _resolved.get("finance")
        except Exception:
            workflow.logger.warning("gmail_ingest_finance_resolve_failed")
            finance_agent = None

        for ch in channels:
            identifier = ch["identifier"]
            label = (ch.get("config") or {}).get("label", identifier)
            since_cursor = (ch.get("config") or {}).get("last_cursor_ts")

            fetched = await self._fetch_with_reauth(input, label, identifier, since_cursor)
            if fetched is None:
                per_account.append({"account": label, "status": "auth_failed"})
                continue

            processed_here = 0
            for msg in fetched.messages:
                # Idempotency claim
                new = await workflow.execute_activity(
                    "ingest_idempotency_claim",
                    args=["gmail", msg["id"]],
                    start_to_close_timeout=_ACT_TIMEOUT,
                    retry_policy=ACT_RETRY,
                )
                if not new:
                    continue

                # Fetch full thread body for richer classification context.
                thread_content = ""
                if msg.get("thread_id"):
                    try:
                        thread_content = await workflow.execute_activity(
                            "fetch_thread",
                            args=[label, msg["thread_id"]],
                            start_to_close_timeout=_ACT_TIMEOUT,
                            retry_policy=NO_RETRY,
                        )
                    except Exception as _te:
                        workflow.logger.warning("fetch_thread_skipped: %s", str(_te)[:200])

                try:
                    classification = await workflow.execute_activity(
                        "classify_email",
                        args=[msg, thread_content],
                        start_to_close_timeout=_CLASSIFY_TIMEOUT,
                        retry_policy=RetryPolicy(maximum_attempts=2),
                    )
                except Exception as classify_exc:
                    # LLM unavailable — log and continue with informational
                    # fallback so the run doesn't abort all remaining emails.
                    workflow.logger.warning(
                        "classify_email_failed_fallback msg=%s err=%s",
                        msg.get("id", ""),
                        str(classify_exc)[:200],
                    )
                    classification = {"category": "informational", "confidence": 0.0, "tags": []}
                # Throttle ONLY when an LLM call actually happened — cache and
                # Gmail-signal hits are instant and need no pause (the whole
                # point of the data-driven cascade).
                source = classification.get("source", "?")
                if source == "llm":
                    await workflow.sleep(_LLM_THROTTLE_SECS)
                category = classification.get("category", "informational")
                by_category[category] = by_category.get(category, 0) + 1
                by_source[source] = by_source.get(source, 0) + 1

                await self._route(input, label, msg, category, classification, thread_content)

                # Tag-based fan-out — additive, orthogonal to category. Currently
                # only `financial`/`payments` spawn a child (MoneyProcessFlow);
                # additional fan-outs would live here too. Fire-and-forget with
                # ParentClosePolicy.ABANDON so child failures don't bubble into
                # the triage run.
                tag_set = {t for t in (classification.get("tags") or []) if isinstance(t, str)}
                if tag_set & {"financial", "payments"} and finance_agent:
                    try:
                        await workflow.start_child_workflow(
                            MoneyProcessFlow.run,
                            MoneyProcessInput(
                                agent_id=finance_agent,
                                msg=msg,
                                account_label=label,
                            ),
                            id=f"money-process-{msg['id']}",
                            parent_close_policy=ParentClosePolicy.ABANDON,
                        )
                    except Exception as exc:
                        workflow.logger.warning(
                            "money_fanout_start_failed msg=%s err=%s",
                            msg.get("id", ""),
                            str(exc)[:200],
                        )

                processed_here += 1
                total_processed += 1

            # Advance cursor for the account (only if fetched returned a valid date)
            if fetched.latest_internal_date_ms > 0:
                latest_iso = _dt.datetime.fromtimestamp(
                    fetched.latest_internal_date_ms / 1000,
                    tz=_dt.UTC,
                ).isoformat()
                await workflow.execute_activity(
                    "update_channel_config_key",
                    args=["email", identifier, "last_cursor_ts", latest_iso],
                    start_to_close_timeout=_ACT_TIMEOUT,
                    retry_policy=ACT_RETRY,
                )

            per_account.append(
                {
                    "account": label,
                    "processed": processed_here,
                    "fetched": len(fetched.messages),
                }
            )

        return {
            "processed": total_processed,
            "by_category": by_category,
            "by_source": by_source,
            "accounts": per_account,
        }

    async def _fetch_with_reauth(
        self,
        input: GmailIngestInput,
        label: str,
        identifier: str,
        since_cursor: str | None,
    ) -> FetchEmailsResult | None:
        """Fetch emails; on auth expired spawn InteractionFlow and retry once."""
        try:
            return await workflow.execute_activity(
                "fetch_emails",
                FetchEmailsInput(
                    account_label=label,
                    query=input.query,
                    since_cursor_ts=since_cursor,
                    max_results=input.max_per_account,
                ),
                result_type=FetchEmailsResult,
                start_to_close_timeout=_FETCH_TIMEOUT,
                retry_policy=NO_RETRY,
            )
        except Exception as exc:
            # Temporal wraps ApplicationError inside ActivityError; walk the
            # cause chain to detect our non-retryable sentinel.
            if not is_auth_expired(exc):
                raise

            workflow.logger.warning("gmail_auth_expired label=%s — pausing for reauth", label)
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
                    prompt=f"Gmail auth expired for <b>{_esc(label)}</b>. Tap below to reauth.",
                    options={"url": url_template, "button_label": "🔐 Reauth Gmail"},
                    timeout_seconds=86400,
                    timeout_policy="hold",
                ),
                id=f"gmail-reauth-{label}-{workflow.info().workflow_id}",
            )
            if result.status != "resolved":
                return None

            # Retry fetch once after reauth
            try:
                return await workflow.execute_activity(
                    "fetch_emails",
                    FetchEmailsInput(
                        account_label=label,
                        query=input.query,
                        since_cursor_ts=since_cursor,
                        max_results=input.max_per_account,
                    ),
                    result_type=FetchEmailsResult,
                    start_to_close_timeout=_FETCH_TIMEOUT,
                    retry_policy=NO_RETRY,
                )
            except Exception as retry_exc:
                workflow.logger.warning(
                    "gmail_fetch_retry_failed label=%s err=%s",
                    label,
                    str(retry_exc)[:200],
                )
                return None

    async def _route(
        self,
        input: GmailIngestInput,
        label: str,
        msg: dict,
        category: str,
        classification: dict | None = None,
        thread_content: str = "",
    ) -> str:
        """Act on the email per its category. Returns the action string."""
        # Feedback loop: log this prediction and, on any later re-observation of
        # the same email, capture a user Gmail-label correction into
        # triage_accuracy (the mis-triage signal). Fire-and-forget.
        try:
            await workflow.execute_activity(
                "record_triage_outcome",
                args=[msg.get("id", ""), category, msg.get("labels") or []],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
        except Exception:
            pass

        # Important emails (action + read) land in the knowledge graph
        # so Raphael's search/ask tools can recall them later. Fire-and-
        # forget — ingest failures don't block the route's primary action.
        if category in {"important_action", "important_read"} and classification:
            try:
                await workflow.execute_activity(
                    "ingest_email_to_kg",
                    args=[msg, thread_content, classification],
                    start_to_close_timeout=TIMEOUT_STANDARD,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:
                workflow.logger.warning(
                    "gmail_ingest_to_kg_failed msg_id=%s err=%s",
                    msg.get("id", ""),
                    str(exc)[:200],
                )

        if category == "important_action":
            # Phase 2: replace the InteractionFlow approval card with a
            # Todoist Inbox capture. Urgent security/payments emails ALSO
            # get a fire-and-forget chat notification.
            cl = classification or {}
            tags_set = set(cl.get("tags") or [])
            msg_id = msg["id"]
            subject = msg.get("subject", "(no subject)")
            sender = msg.get("sender", "unknown sender")
            reason = cl.get("reason", "")
            summary = cl.get("summary", "")
            deeplink = msg.get("permalink") or f"https://mail.google.com/mail/u/0/#inbox/{msg_id}"
            # Description chain: From + LLM summary + Open-in-Gmail link, plus
            # body excerpt OR snippet as a separator-delimited tail. The
            # @reference flow needs a substantive body downstream — prefer the
            # fetched thread, fall back to Gmail's snippet preview, and have
            # the LLM-generated `summary` always present so the description
            # is never bare even when the body never made it back.
            body_excerpt = thread_content[:4000] if thread_content else (msg.get("snippet") or "")
            lane = cl.get("lane") or msg.get("lane") or "own"
            header_lines = [f"From: {sender}"]
            if lane != "own":
                # Surface forwarding provenance up-front so the user (and
                # ClarifyFlow) can tell *which* mailbox identity this
                # landed in without having to dig through headers.
                header_lines.append(f"Forwarded from: {lane}")
            if summary:
                header_lines.append(summary)
            elif reason:
                # Old classifications (pre-summary) or an LLM hiccup — keep
                # the one-line reason rather than leaving description bare.
                header_lines.append(reason)
            # Pull related prior context from KS so the task is pointed, not
            # just "an email arrived". Best-effort — never blocks the capture.
            try:
                related = await workflow.execute_activity(
                    "gather_email_context",
                    args=[subject, sender, deeplink],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:
                workflow.logger.warning(
                    "gmail_email_context_failed msg_id=%s err=%s", msg_id, str(exc)[:200]
                )
                related = ""
            if related:
                header_lines.append(f"Related context:\n{related}")
            header_lines.append(f"[Open in Gmail]({deeplink})")
            description = "\n\n".join(header_lines)
            if body_excerpt:
                description += f"\n\n---\n\n{body_excerpt}"
            capture_ref: str | None = None
            capture_failed = False
            try:
                capture_ref = await workflow.execute_activity_method(
                    CaptureActivities.capture_to_inbox,
                    args=["#email", f"gmail-{msg_id}", subject, description],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:
                capture_failed = True
                workflow.logger.warning(
                    "gmail_capture_failed msg_id=%s err=%s",
                    msg_id,
                    str(exc)[:200],
                )
            # `capture_to_inbox` returns None on kill-switch / no-inbox /
            # permanent rejection.
            if capture_failed or capture_ref is None:
                return "capture_failed"

            # Chat escalation for high-confidence security/payments
            confidence = float(cl.get("confidence") or 0.0)
            if confidence > 0.9 and ("security" in tags_set or "payments" in tags_set):
                try:
                    await workflow.execute_activity_method(
                        DeliveryActivities.send_system_event,
                        args=[
                            f"⚠️ Urgent email captured to Inbox: {subject[:80]}\nFrom: {sender}",
                        ],
                        start_to_close_timeout=TIMEOUT_FAST,
                        retry_policy=NO_RETRY,
                    )
                except Exception as exc:
                    workflow.logger.warning(
                        "gmail_escalate_failed msg_id=%s err=%s",
                        msg_id,
                        str(exc)[:200],
                    )
            # Surface in Gmail too: apply the IMPORTANT label, keep unread.
            # Best-effort — the Todoist capture above is the durable record.
            try:
                await workflow.execute_activity(
                    "apply_label",
                    args=[label, msg_id, "IMPORTANT"],
                    start_to_close_timeout=_ACT_TIMEOUT,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:
                workflow.logger.warning(
                    "gmail_label_important_failed msg_id=%s err=%s", msg_id, str(exc)[:200]
                )
            return "captured_to_inbox"

        if category == "important_read":
            # Surface via Gmail's IMPORTANT label and KEEP UNREAD. No per-email
            # chat ping — the label is the surface now (pinging every
            # receipt/GitHub notice is noise; 2026-05-30 redesign).
            label_result = await workflow.execute_activity(
                "apply_label",
                args=[label, msg["id"], "IMPORTANT"],
                start_to_close_timeout=_ACT_TIMEOUT,
                retry_policy=NO_RETRY,
            )
            if not (label_result or {}).get("ok"):
                workflow.logger.warning(
                    "apply_label_failed account=%s msg_id=%s label=IMPORTANT result=%s",
                    label,
                    msg["id"],
                    label_result,
                )
                return "label_failed"
            return "labeled_important"

        if category == "informational":
            # Not important → mark read so the inbox clears itself.
            label_result = await workflow.execute_activity(
                "apply_label",
                args=[label, msg["id"], "READ"],
                start_to_close_timeout=_ACT_TIMEOUT,
                retry_policy=NO_RETRY,
            )
            if not (label_result or {}).get("ok"):
                workflow.logger.warning(
                    "apply_label_failed account=%s msg_id=%s label=READ result=%s",
                    label,
                    msg["id"],
                    label_result,
                )
                return "label_failed"
            return "marked_read"

        if category == "useless":
            label_result = await workflow.execute_activity(
                "apply_label",
                args=[label, msg["id"], "READ"],
                start_to_close_timeout=_ACT_TIMEOUT,
                retry_policy=NO_RETRY,
            )
            if not (label_result or {}).get("ok"):
                # User keeps seeing this in is:unread — surface as label_failed
                # so the caller knows we didn't actually mark it read.
                workflow.logger.warning(
                    "apply_label_failed account=%s msg_id=%s label=READ result=%s",
                    label,
                    msg["id"],
                    label_result,
                )
                return "label_failed"
            return "marked_read"

        return "unknown"
