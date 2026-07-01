"""CalendarActivities — event fetch."""

from __future__ import annotations

import json

import pytest
from aegis_worker.activities.calendar import (
    CalendarActivities,
    FetchEventsInput,
    FetchEventsResult,
)
from aegis_worker.activities.gmail import GmailAuthExpiredError
from temporalio.testing import ActivityEnvironment


class _FakeReq:
    def __init__(self, payload, raise_auth=False):
        self._payload = payload
        self._raise_auth = raise_auth

    def execute(self):
        if self._raise_auth:
            from google.auth.exceptions import RefreshError

            raise RefreshError("invalid_grant")
        return self._payload


class _FakeCalendarService:
    def __init__(self, items, raise_auth=False):
        self._items = items
        self._raise_auth = raise_auth

    def events(self):
        return self

    def list(self, **kwargs):
        return _FakeReq({"items": self._items}, self._raise_auth)


@pytest.fixture
def cal(tmp_path):
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    (tokens / "sebas.json").write_text(
        json.dumps(
            {
                "token": "tok",
                "refresh_token": "rt",
                "client_id": "cid",
                "client_secret": "cs",
                "token_uri": "https://oauth2.googleapis.com/token",
                "scopes": ["https://www.googleapis.com/auth/calendar.readonly"],
            }
        )
    )
    creds = tmp_path / "credentials.json"
    creds.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "cid",
                    "client_secret": "cs",
                    "redirect_uris": ["http://localhost/cb"],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        )
    )
    return CalendarActivities(
        gmail_credentials_file=str(creds),
        gmail_token_dir=str(tokens),
        aegis_ui_url="https://aegis.example.com",
    )


@pytest.mark.asyncio
async def test_fetch_events_returns_events(cal, monkeypatch):
    fake = _FakeCalendarService(
        [
            {
                "id": "e1",
                "summary": "Standup",
                "start": {"dateTime": "2026-04-20T09:00:00Z"},
                "end": {"dateTime": "2026-04-20T09:30:00Z"},
                "status": "confirmed",
                "updated": "2026-04-18T10:00:00Z",
                "attendees": [{"email": "a@b.com"}],
            },
            {
                "id": "e2",
                "summary": "Lunch",
                "start": {"date": "2026-04-21"},
                "end": {"date": "2026-04-21"},
                "status": "confirmed",
                "updated": "2026-04-18T11:00:00Z",
            },
        ]
    )
    import aegis_worker.activities.calendar as cal_mod

    monkeypatch.setattr(cal_mod, "_build_calendar_service", lambda *a, **k: fake)
    env = ActivityEnvironment()
    result = await env.run(
        cal.fetch_events,
        FetchEventsInput(account_label="sebas", since_cursor_ts=None, horizon_days=30),
    )
    assert isinstance(result, FetchEventsResult)
    assert len(result.events) == 2
    assert result.events[0]["summary"] == "Standup"
    assert result.events[1]["start"] == "2026-04-21"
    assert result.latest_updated_ts == "2026-04-18T11:00:00Z"


@pytest.mark.asyncio
async def test_fetch_events_since_cursor(cal, monkeypatch):
    """since_cursor_ts is forwarded to the service (tested via passthrough kwargs)."""
    captured_kwargs: dict = {}

    class _CapturingService:
        def events(self):
            return self

        def list(self, **kwargs):
            captured_kwargs.update(kwargs)
            return _FakeReq({"items": []})

    import aegis_worker.activities.calendar as cal_mod

    monkeypatch.setattr(cal_mod, "_build_calendar_service", lambda *a, **k: _CapturingService())
    env = ActivityEnvironment()
    result = await env.run(
        cal.fetch_events,
        FetchEventsInput(
            account_label="sebas",
            since_cursor_ts="2026-04-01T00:00:00Z",
            horizon_days=7,
        ),
    )
    assert result.events == []
    assert result.latest_updated_ts is None
    assert "updatedMin" in captured_kwargs
    assert captured_kwargs["updatedMin"] == "2026-04-01T00:00:00Z"


@pytest.mark.asyncio
async def test_fetch_events_no_cursor_omits_updated_min(cal, monkeypatch):
    """When since_cursor_ts is None, updatedMin must not be passed."""
    captured_kwargs: dict = {}

    class _CapturingService:
        def events(self):
            return self

        def list(self, **kwargs):
            captured_kwargs.update(kwargs)
            return _FakeReq({"items": []})

    import aegis_worker.activities.calendar as cal_mod

    monkeypatch.setattr(cal_mod, "_build_calendar_service", lambda *a, **k: _CapturingService())
    env = ActivityEnvironment()
    await env.run(
        cal.fetch_events,
        FetchEventsInput(account_label="sebas", since_cursor_ts=None, horizon_days=30),
    )
    assert "updatedMin" not in captured_kwargs


@pytest.mark.asyncio
async def test_fetch_events_auth_expired(cal, monkeypatch):
    fake = _FakeCalendarService([], raise_auth=True)
    import aegis_worker.activities.calendar as cal_mod

    monkeypatch.setattr(cal_mod, "_build_calendar_service", lambda *a, **k: fake)
    env = ActivityEnvironment()
    with pytest.raises(GmailAuthExpiredError) as exc_info:
        await env.run(
            cal.fetch_events,
            FetchEventsInput(account_label="sebas", since_cursor_ts=None, horizon_days=30),
        )
    assert exc_info.value.account_label == "sebas"
    assert "reauth/sebas/initiate" in exc_info.value.reauth_url


@pytest.mark.asyncio
async def test_fetch_events_attendees_and_html_link(cal, monkeypatch):
    """Attendees list and html_link are mapped correctly."""
    fake = _FakeCalendarService(
        [
            {
                "id": "e1",
                "summary": "Review",
                "start": {"dateTime": "2026-04-20T14:00:00Z"},
                "end": {"dateTime": "2026-04-20T15:00:00Z"},
                "status": "confirmed",
                "updated": "2026-04-18T12:00:00Z",
                "attendees": [{"email": "x@y.com"}, {"email": "z@y.com"}],
                "htmlLink": "https://calendar.google.com/event?eid=xxx",
                "description": "Quarterly review",
            },
        ]
    )
    import aegis_worker.activities.calendar as cal_mod

    monkeypatch.setattr(cal_mod, "_build_calendar_service", lambda *a, **k: fake)
    env = ActivityEnvironment()
    result = await env.run(
        cal.fetch_events,
        FetchEventsInput(account_label="sebas", since_cursor_ts=None, horizon_days=30),
    )
    ev = result.events[0]
    assert ev["attendees"] == ["x@y.com", "z@y.com"]
    assert ev["html_link"] == "https://calendar.google.com/event?eid=xxx"
    assert ev["description"] == "Quarterly review"
