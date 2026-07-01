"""Tests for surfacing collected knowledge:
- the briefing's new `collected` section (references filed → daily briefing)
- Gmail task enrichment via gather_email_context (KS related-context lookup)
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from aegis_worker.activities.briefing import BriefingActivities
from aegis_worker.activities.gmail import GmailActivities
from temporalio.testing import ActivityEnvironment


def _iso(dt: datetime) -> str:
    return dt.replace(tzinfo=UTC).isoformat()


# ── briefing: collected section ──────────────────────────────────────────────


def _briefing_kc(reference_items: list[dict]) -> AsyncMock:
    kc = AsyncMock()

    async def _list(limit: int = 200, source_type: str | None = None):
        return list(reference_items) if source_type == "reference" else []

    kc.list_content_items = AsyncMock(side_effect=_list)
    kc.search = AsyncMock(return_value=[])  # intel gather → empty
    return kc


@pytest.mark.asyncio
async def test_briefing_surfaces_collected_references():
    now = datetime.now(UTC)
    kc = _briefing_kc(
        [
            {"content_id": "r1", "title": "A blog post I saved", "source_type": "reference",
             "ingested_at": _iso(now - timedelta(hours=2))},
            {"content_id": "r2", "title": "Stale, out of window", "source_type": "reference",
             "ingested_at": _iso(now - timedelta(hours=72))},
        ]
    )
    act = BriefingActivities(db_pool=None, knowledge_connector=kc)
    changes = await ActivityEnvironment().run(act.gather_briefing_changes)
    titles = [c["title"] for c in changes["collected"]]
    assert titles == ["A blog post I saved"]  # stale one filtered by window
    assert changes["quiet"] is False  # collected alone makes it non-quiet


def test_fallback_formatter_renders_collected():
    act = BriefingActivities()
    out = act._format_changes_fallback(
        {"collected": [{"title": "Saved article on X"}], "intel": [], "broke": {}, "calendar": {}}
    )
    assert "Came across your feeds" in out
    assert "Saved article on X" in out


# ── gmail: related-context enrichment ────────────────────────────────────────


def _gmail_kc(hits: list[dict]) -> AsyncMock:
    kc = AsyncMock()
    kc.search = AsyncMock(return_value=hits)
    return kc


@pytest.mark.asyncio
async def test_email_context_returns_related_excludes_self():
    kc = _gmail_kc(
        [
            {"title": "Prior thread on the contract", "url": "ks://a", "score": 0.8},
            {"title": "THIS email", "url": "https://mail.google.com/self", "score": 0.9},
            {"title": "Unrelated noise", "url": "ks://b", "score": 0.10},  # below floor
        ]
    )
    act = GmailActivities(gmail_credentials_file="", gmail_token_dir="", knowledge_connector=kc)
    out = await ActivityEnvironment().run(
        act.gather_email_context, "Contract renewal", "legal@x.com", "https://mail.google.com/self"
    )
    assert "Prior thread on the contract" in out
    assert "THIS email" not in out       # excluded by url
    assert "Unrelated noise" not in out  # below score floor


@pytest.mark.asyncio
async def test_email_context_no_connector_is_empty():
    act = GmailActivities(gmail_credentials_file="", gmail_token_dir="", knowledge_connector=None)
    out = await ActivityEnvironment().run(
        act.gather_email_context, "subj", "sender", ""
    )
    assert out == ""


@pytest.mark.asyncio
async def test_email_context_swallows_search_error():
    kc = AsyncMock()
    kc.search = AsyncMock(side_effect=Exception("ks down"))
    act = GmailActivities(gmail_credentials_file="", gmail_token_dir="", knowledge_connector=kc)
    out = await ActivityEnvironment().run(act.gather_email_context, "subj", "sender", "")
    assert out == ""
