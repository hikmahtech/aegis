"""Google Calendar event fetch activity. Shares OAuth token files with GmailActivities."""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import structlog
from temporalio import activity

from aegis_worker.activities.gmail import GmailAuthExpiredError

logger = structlog.get_logger()


@dataclass
class FetchEventsInput:
    account_label: str
    since_cursor_ts: str | None  # ISO timestamp; if None, no updatedMin filter
    horizon_days: int = 30


@dataclass
class FetchEventsResult:
    events: list[dict] = field(default_factory=list)
    latest_updated_ts: str | None = None


def _build_calendar_service(creds_file: str, token_path: Path):
    """Build a googleapiclient Calendar service. Separated so tests can monkeypatch."""
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if not token_path.exists():
        raise RefreshError(f"token_missing:{token_path}")
    creds = Credentials.from_authorized_user_file(str(token_path))
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request as GoogleRequest

        creds.refresh(GoogleRequest())
        token_path.write_text(creds.to_json())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


@dataclass
class CalendarActivities:
    gmail_credentials_file: str  # same OAuth credentials file as GmailActivities
    gmail_token_dir: str  # same token directory as GmailActivities
    aegis_ui_url: str = ""

    @activity.defn
    async def fetch_events(self, input: FetchEventsInput) -> FetchEventsResult:
        """Fetch calendar events for the given account. Raises GmailAuthExpiredError on token failure."""
        token_path = Path(self.gmail_token_dir) / f"{input.account_label}.json"

        def _sync_fetch() -> FetchEventsResult:
            from google.auth.exceptions import RefreshError

            try:
                svc = _build_calendar_service(self.gmail_credentials_file, token_path)
                now = dt.datetime.now(dt.UTC)
                time_min = now.isoformat()
                time_max = (now + dt.timedelta(days=input.horizon_days)).isoformat()

                list_kwargs: dict = {
                    "calendarId": "primary",
                    "timeMin": time_min,
                    "timeMax": time_max,
                    "maxResults": 100,
                    "orderBy": "updated",
                    "singleEvents": True,
                }
                if input.since_cursor_ts is not None:
                    list_kwargs["updatedMin"] = input.since_cursor_ts

                response = svc.events().list(**list_kwargs).execute()
                raw_events = response.get("items") or []

                events: list[dict] = []
                for e in raw_events:
                    start_obj = e.get("start") or {}
                    end_obj = e.get("end") or {}
                    events.append(
                        {
                            "id": e["id"],
                            "summary": e.get("summary", ""),
                            "description": e.get("description", ""),
                            "start": start_obj.get("dateTime") or start_obj.get("date", ""),
                            "end": end_obj.get("dateTime") or end_obj.get("date", ""),
                            "attendees": [a.get("email", "") for a in e.get("attendees") or []],
                            "status": e.get("status", ""),
                            "updated": e.get("updated", ""),
                            "html_link": e.get("htmlLink", ""),
                        }
                    )

                latest_updated_ts: str | None = None
                if events:
                    latest_updated_ts = max(ev["updated"] for ev in events)

                return FetchEventsResult(events=events, latest_updated_ts=latest_updated_ts)

            except RefreshError as exc:
                reauth_url = (
                    f"{self.aegis_ui_url.rstrip('/')}"
                    f"/api/admin/gmail/reauth/{input.account_label}/initiate"
                )
                raise GmailAuthExpiredError(input.account_label, reauth_url) from exc

        return await asyncio.to_thread(_sync_fetch)

    @activity.defn
    async def events_to_content(self, events: list[dict]) -> list[dict]:
        """Convert a batch of calendar events to content dicts.

        ponytail: graph layer removed. Imports core helper for event→content conversion.
        """
        try:
            from aegis.services.claims import calendar_event_to_content
        except ImportError:
            activity.logger.warning("calendar_event_to_content_import_failed")
            return []
        return [calendar_event_to_content(e) for e in events]
