"""SentryIngestActivities — fetch, convert, cursor."""

from __future__ import annotations

import pytest
import respx
from aegis_worker.activities.sentry_ingest import (
    FetchNewIssuesInput,
    FetchNewIssuesResult,
    SentryIngestActivities,
)
from httpx import Response
from temporalio.testing import ActivityEnvironment


@pytest.fixture
def sentry_act():
    return SentryIngestActivities(
        db_pool=None,
        sentry_url="https://sentry.example.com",
        sentry_token="tok",
        sentry_org="org-x",
    )


@pytest.mark.asyncio
@respx.mock
async def test_fetch_new_issues_returns_list(sentry_act):
    respx.get("https://sentry.example.com/api/0/organizations/org-x/issues/").mock(
        return_value=Response(
            200,
            json=[
                {
                    "id": "2",
                    "title": "Error B",
                    "level": "error",
                    "project": {"slug": "app"},
                    "firstSeen": "2026-04-18T11:00:00Z",
                },
                {
                    "id": "1",
                    "title": "Error A",
                    "level": "error",
                    "project": {"slug": "app"},
                    "firstSeen": "2026-04-18T10:00:00Z",
                },
            ],
        )
    )
    env = ActivityEnvironment()
    result = await env.run(
        sentry_act.fetch_new_issues,
        FetchNewIssuesInput(since_issue_id=None),
    )
    assert isinstance(result, FetchNewIssuesResult)
    assert len(result.issues) == 2
    assert result.latest_issue_id == "2"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_new_issues_filters_by_since(sentry_act):
    respx.get("https://sentry.example.com/api/0/organizations/org-x/issues/").mock(
        return_value=Response(
            200,
            json=[
                {"id": "3", "title": "C", "level": "error", "project": {"slug": "app"}},
                {"id": "2", "title": "B", "level": "error", "project": {"slug": "app"}},
                {"id": "1", "title": "A", "level": "error", "project": {"slug": "app"}},
            ],
        )
    )
    env = ActivityEnvironment()
    result = await env.run(
        sentry_act.fetch_new_issues,
        FetchNewIssuesInput(since_issue_id="2"),
    )
    # Should drop id=2 and everything at/before it; leave only id=3
    assert len(result.issues) == 1
    assert result.issues[0]["id"] == "3"


@pytest.mark.asyncio
async def test_issue_to_alert_shape(sentry_act):
    env = ActivityEnvironment()
    alert = await env.run(
        sentry_act.issue_to_alert,
        {
            "id": "42",
            "title": "KeyError: foo",
            "culprit": "module.function",
            "level": "error",
            "project": {"slug": "web"},
            "environment": "prod",
            "platform": "python",
            "metadata": {"value": "foo is not set"},
        },
    )
    assert alert["source"] == "sentry"
    assert alert["fingerprint"] == "sentry:42"
    assert alert["severity"] == "error"
    assert alert["service"] == "web"
    assert alert["labels"]["environment"] == "prod"
    assert "foo is not set" in alert["description"]


@pytest.mark.asyncio
async def test_empty_sentry_url_returns_empty():
    a = SentryIngestActivities(
        db_pool=None,
        sentry_url="",
        sentry_token="",
        sentry_org="",
    )
    env = ActivityEnvironment()
    result = await env.run(
        a.fetch_new_issues,
        FetchNewIssuesInput(since_issue_id=None),
    )
    assert result.issues == []


@pytest.mark.asyncio
async def test_issue_to_alert_null_fields(sentry_act):
    """Handles None/missing optional fields gracefully."""
    env = ActivityEnvironment()
    alert = await env.run(
        sentry_act.issue_to_alert,
        {
            "id": "99",
            "title": "Bare error",
            "level": "warning",
            # no culprit, project, environment, platform, metadata
        },
    )
    assert alert["fingerprint"] == "sentry:99"
    assert alert["severity"] == "warning"
    assert alert["service"] == ""
    assert alert["labels"]["environment"] == ""
    assert alert["labels"]["platform"] == ""
    assert alert["description"] == ""


@pytest.mark.asyncio
async def test_read_sentry_cursor_no_pool():
    a = SentryIngestActivities(db_pool=None, sentry_url="", sentry_token="", sentry_org="")
    env = ActivityEnvironment()
    result = await env.run(a.read_sentry_cursor)
    assert result is None


@pytest.mark.asyncio
async def test_read_sentry_cursor_with_pool():
    from unittest.mock import AsyncMock, MagicMock

    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"last_issue_id": "123"})
    a = SentryIngestActivities(db_pool=pool, sentry_url="", sentry_token="", sentry_org="")
    env = ActivityEnvironment()
    result = await env.run(a.read_sentry_cursor)
    assert result == "123"


@pytest.mark.asyncio
async def test_read_sentry_cursor_missing_row():
    from unittest.mock import AsyncMock, MagicMock

    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    a = SentryIngestActivities(db_pool=pool, sentry_url="", sentry_token="", sentry_org="")
    env = ActivityEnvironment()
    result = await env.run(a.read_sentry_cursor)
    assert result is None


@pytest.mark.asyncio
async def test_write_sentry_cursor_no_pool():
    """write_sentry_cursor is a no-op when db_pool is None."""
    a = SentryIngestActivities(db_pool=None, sentry_url="", sentry_token="", sentry_org="")
    env = ActivityEnvironment()
    # Should not raise
    await env.run(a.write_sentry_cursor, "456")


@pytest.mark.asyncio
async def test_write_sentry_cursor_with_pool():
    from unittest.mock import AsyncMock, MagicMock

    pool = MagicMock()
    pool.execute = AsyncMock()
    a = SentryIngestActivities(db_pool=pool, sentry_url="", sentry_token="", sentry_org="")
    env = ActivityEnvironment()
    await env.run(a.write_sentry_cursor, "789")
    pool.execute.assert_called_once()
    call_args = pool.execute.call_args
    assert "789" in call_args.args


# ingest_idempotency_claim was removed from SentryIngestActivities —
# ChannelActivities.ingest_idempotency_claim is the shared implementation
# every ingest flow uses. See its tests for coverage.


@pytest.mark.asyncio
@respx.mock
async def test_fetch_new_issues_latest_id_with_since_filter(sentry_act):
    """latest_issue_id reflects first raw item even when some are filtered."""
    respx.get("https://sentry.example.com/api/0/organizations/org-x/issues/").mock(
        return_value=Response(
            200,
            json=[
                {"id": "5", "title": "E", "level": "error", "project": {"slug": "app"}},
                {"id": "4", "title": "D", "level": "error", "project": {"slug": "app"}},
                {"id": "3", "title": "C", "level": "error", "project": {"slug": "app"}},
            ],
        )
    )
    env = ActivityEnvironment()
    result = await env.run(
        sentry_act.fetch_new_issues,
        FetchNewIssuesInput(since_issue_id="4"),
    )
    assert len(result.issues) == 1
    assert result.issues[0]["id"] == "5"
    # latest_issue_id always comes from the first raw item
    assert result.latest_issue_id == "5"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_new_issues_scopes_by_projects():
    """When sentry_projects is set, each project ID appears as a separate project= param."""
    route = respx.get("https://sentry.example.com/api/0/organizations/org-x/issues/").mock(
        return_value=Response(
            200,
            json=[{"id": "10", "title": "T", "level": "error", "project": {"slug": "app"}}],
        )
    )
    act = SentryIngestActivities(
        db_pool=None,
        sentry_url="https://sentry.example.com",
        sentry_token="tok",
        sentry_org="org-x",
        sentry_projects=[111, 222],
    )
    env = ActivityEnvironment()
    await env.run(act.fetch_new_issues, FetchNewIssuesInput(since_issue_id=None))
    assert route.called
    request = route.calls.last.request
    # httpx encodes list values as repeated query params: project=111&project=222
    assert "project=111" in str(request.url)
    assert "project=222" in str(request.url)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_new_issues_no_project_filter_when_list_empty():
    """When sentry_projects is empty, no project= param is sent."""
    route = respx.get("https://sentry.example.com/api/0/organizations/org-x/issues/").mock(
        return_value=Response(200, json=[])
    )
    act = SentryIngestActivities(
        db_pool=None,
        sentry_url="https://sentry.example.com",
        sentry_token="tok",
        sentry_org="org-x",
        sentry_projects=[],
    )
    env = ActivityEnvironment()
    await env.run(act.fetch_new_issues, FetchNewIssuesInput(since_issue_id=None))
    assert route.called
    request = route.calls.last.request
    assert "project=" not in str(request.url)
