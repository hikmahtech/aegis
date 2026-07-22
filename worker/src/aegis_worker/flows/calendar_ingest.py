"""CalendarIngestFlow — daily calendar event ingestion into knowledge-service.

Iterates email channels (calendar uses the same Google OAuth tokens as Gmail).
Per account: fetch_events → events_to_content → ingest_content. Cursor kept in
channels.config.calendar_last_cursor_ts.

Auth-expired handling: calendar shares Gmail's GmailAuthExpiredError sentinel
(activities/calendar.py raises it on token refresh failure). On detection we
spawn the same InteractionFlow(kind='ack', timeout_policy='hold') reauth card
as GmailIngestFlow and retry the fetch once after the user resolves.

ponytail: graph layer removed. Converts events to content (chunk+embed) instead of claims.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from html import escape as _esc

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.calendar import FetchEventsInput, FetchEventsResult
    from aegis_worker.activities.capture import CaptureActivities
    from aegis_worker.flows.interaction import InteractionFlow, InteractionFlowInput
    from aegis_worker.shared.gmail_auth import is_auth_expired
    from aegis_worker.shared.retry import ACT_RETRY, NO_RETRY


_ACTION_LANGUAGE_RE = re.compile(
    r"\b(rsvp|prepare|submit|due|deadline|register|confirm)\b",
    re.IGNORECASE,
)


_ACT_TIMEOUT = timedelta(seconds=60)
_FETCH_TIMEOUT = timedelta(seconds=120)


@dataclass
class CalendarIngestInput:
    agent_id: str = "sebas"
    horizon_days: int = 30
    aegis_ui_url: str = ""


@workflow.defn(name="CalendarIngestFlow")
class CalendarIngestFlow:
    @workflow.run
    async def run(self, input: CalendarIngestInput) -> dict:
        channels = await workflow.execute_activity(
            "list_active_channels",
            "email",
            start_to_close_timeout=_ACT_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        total_events = 0
        total_content = 0
        total_ingested = 0
        errors = 0
        # Labeled per-account so a soft-failing account is visible instead of
        # hiding behind an unlabeled `errors: 1` (issue #120).
        errors_by_account: dict[str, int] = {}

        for ch in channels:
            identifier = ch["identifier"]
            label = (ch.get("config") or {}).get("label", identifier)
            since_cursor = (ch.get("config") or {}).get("calendar_last_cursor_ts")

            result = await self._fetch_with_reauth(input, label, since_cursor)
            if result is None:
                errors += 1
                errors_by_account[label] = errors_by_account.get(label, 0) + 1
                continue

            if not result.events:
                continue

            total_events += len(result.events)

            for ev in result.events:
                title = ev.get("summary") or ""
                notes = ev.get("description") or ""
                if not _ACTION_LANGUAGE_RE.search(title) and not _ACTION_LANGUAGE_RE.search(notes):
                    continue
                event_id = ev.get("id")
                if not event_id:
                    continue
                start = ev.get("start", "?")
                location = ev.get("location", "")
                event_url = ev.get("html_link") or ""
                capture_description = (
                    f"{start}\n{location}\n\n[Open in Calendar]({event_url})"
                    if event_url
                    else f"{start}\n{location}"
                )
                try:
                    await workflow.execute_activity_method(
                        CaptureActivities.capture_to_inbox,
                        args=["#calendar", f"calendar-{event_id}", title[:120], capture_description],
                        start_to_close_timeout=_ACT_TIMEOUT,
                        retry_policy=NO_RETRY,
                    )
                except Exception as exc:
                    workflow.logger.warning(
                        "calendar_capture_failed event_id=%s err=%s",
                        event_id,
                        str(exc)[:200],
                    )

            content_items = await workflow.execute_activity(
                "events_to_content",
                result.events,
                start_to_close_timeout=_ACT_TIMEOUT,
                retry_policy=ACT_RETRY,
            )
            total_content += len(content_items)

            if content_items:
                for item in content_items:
                    try:
                        ingest_result = await workflow.execute_activity(
                            "ingest_content",
                            item,
                            start_to_close_timeout=timedelta(seconds=180),
                            retry_policy=ACT_RETRY,
                        )
                        if ingest_result.get("status") in ("ok", "accepted"):
                            total_ingested += 1
                    except Exception as exc:
                        workflow.logger.warning(
                            "calendar_ingest_content_failed err=%s", str(exc)[:200]
                        )

            if result.latest_updated_ts:
                await workflow.execute_activity(
                    "update_channel_config_key",
                    args=["email", identifier, "calendar_last_cursor_ts", result.latest_updated_ts],
                    start_to_close_timeout=_ACT_TIMEOUT,
                    retry_policy=ACT_RETRY,
                )

        result_summary: dict = {
            "events": total_events,
            "content": total_content,
            "ingested": total_ingested,
            "errors": errors,
        }
        if errors_by_account:
            result_summary["errors_by_account"] = errors_by_account
        return result_summary

    async def _fetch_with_reauth(
        self,
        input: CalendarIngestInput,
        label: str,
        since_cursor: str | None,
    ) -> FetchEventsResult | None:
        """Fetch events; on auth expired spawn InteractionFlow(ack) and retry once.

        Mirrors GmailIngestFlow._fetch_with_reauth — calendar shares the
        Google OAuth token so the same reauth UX applies. Returns None when:
          - fetch raises a non-auth error (logged, account skipped)
          - reauth interaction doesn't resolve (e.g. timed out / aborted)
          - the post-reauth retry also fails
        """
        try:
            return await workflow.execute_activity(
                "fetch_events",
                FetchEventsInput(
                    account_label=label,
                    since_cursor_ts=since_cursor,
                    horizon_days=input.horizon_days,
                ),
                result_type=FetchEventsResult,
                start_to_close_timeout=_FETCH_TIMEOUT,
                retry_policy=NO_RETRY,
            )
        except Exception as exc:
            if not is_auth_expired(exc):
                # Network / quota / other transient — log and skip account.
                workflow.logger.warning(
                    "calendar_fetch_failed label=%s err=%s", label, str(exc)[:200]
                )
                return None

            workflow.logger.error(
                "calendar_auth_expired label=%s — pausing for reauth", label
            )
            base = input.aegis_ui_url.rstrip("/")
            # Calendar reauth piggybacks on Gmail's OAuth — same /gmail/reauth
            # endpoint refreshes the shared Google token.
            url_template = (
                f"{base}/api/admin/gmail/reauth/{label}/initiate?interaction_id={{interaction_id}}"
            )
            interaction_result = await workflow.execute_child_workflow(
                InteractionFlow.run,
                InteractionFlowInput(
                    agent_id=input.agent_id,
                    kind="ack",
                    origin="calendar_reauth",
                    prompt=(
                        f"Calendar auth expired for <b>{_esc(label)}</b>. "
                        "Tap below to reauth (shared with Gmail)."
                    ),
                    options={"url": url_template, "button_label": "🔐 Reauth Google"},
                    timeout_seconds=86400,
                    timeout_policy="hold",
                ),
                id=f"calendar-reauth-{label}-{workflow.info().workflow_id}",
            )
            if interaction_result.status != "resolved":
                return None

            try:
                return await workflow.execute_activity(
                    "fetch_events",
                    FetchEventsInput(
                        account_label=label,
                        since_cursor_ts=since_cursor,
                        horizon_days=input.horizon_days,
                    ),
                    result_type=FetchEventsResult,
                    start_to_close_timeout=_FETCH_TIMEOUT,
                    retry_policy=NO_RETRY,
                )
            except Exception as retry_exc:
                workflow.logger.warning(
                    "calendar_fetch_retry_failed label=%s err=%s",
                    label,
                    str(retry_exc)[:200],
                )
                return None
