"""Tests for raphael's references-filed section in the daily briefing."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from aegis_worker.activities.briefing import BriefingActivities
from temporalio.testing import ActivityEnvironment


def _iso(dt: datetime) -> str:
    return dt.replace(tzinfo=UTC).isoformat()


def _make_kc(items_by_source: dict[str, list[dict]]) -> AsyncMock:
    """Build a KS connector mock that returns different items per
    `source_type=` call — mirrors how the activity now calls the KS
    list endpoint once per source_type (reference + intelligence).
    """
    kc = AsyncMock()

    async def _list(limit: int = 200, source_type: str | None = None):
        return list(items_by_source.get(source_type or "", []))

    kc.list_content_items = AsyncMock(side_effect=_list)
    return kc


async def test_gather_references_filed_filters_by_source_type_and_window():
    now = datetime.now(UTC)
    kc = _make_kc(
        {
            "reference": [
                {
                    "content_id": "r1",
                    "title": "Recent reference",
                    "source_type": "reference",
                    "metadata": {"source_tag": "#research"},
                    "ingested_at": _iso(now - timedelta(hours=2)),
                },
                {
                    "content_id": "r2",
                    "title": "Stale reference",
                    "source_type": "reference",
                    "metadata": {"source_tag": "#research"},
                    "ingested_at": _iso(now - timedelta(hours=48)),
                },
            ],
            "intelligence": [],
        }
    )
    act = BriefingActivities(db_pool=None, knowledge_connector=kc)
    env = ActivityEnvironment()
    result = await env.run(act.gather_references_filed, 24)
    assert isinstance(result, list)
    assert [r["content_id"] for r in result] == ["r1"]


async def test_gather_references_filed_includes_intel_scan_items():
    """Briefing digest must include source_type='intelligence' (intel-scan
    auto-ingests) so the user sees automated knowledge ingest signals,
    not just user-initiated reference closures."""
    now = datetime.now(UTC)
    kc = _make_kc(
        {
            "reference": [
                {
                    "content_id": "r1",
                    "title": "Manual reference",
                    "source_type": "reference",
                    "ingested_at": _iso(now - timedelta(hours=2)),
                }
            ],
            "intelligence": [
                {
                    "content_id": "i1",
                    "title": "GPT-5 launched",
                    "source_type": "intelligence",
                    "ingested_at": _iso(now - timedelta(hours=1)),
                }
            ],
        }
    )
    act = BriefingActivities(db_pool=None, knowledge_connector=kc)
    env = ActivityEnvironment()
    result = await env.run(act.gather_references_filed, 24)
    ids = {it["content_id"] for it in result}
    assert ids == {"r1", "i1"}
    # Sorted ingested_at DESC: i1 (1h ago) before r1 (2h ago).
    assert result[0]["content_id"] == "i1"


async def test_gather_references_filed_no_connector_returns_empty():
    act = BriefingActivities(db_pool=None)
    env = ActivityEnvironment()
    result = await env.run(act.gather_references_filed, 24)
    assert result == []


async def test_gather_references_filed_tolerates_connector_failure():
    kc = AsyncMock()
    kc.list_content_items = AsyncMock(side_effect=Exception("KS down"))
    act = BriefingActivities(db_pool=None, knowledge_connector=kc)
    env = ActivityEnvironment()
    result = await env.run(act.gather_references_filed, 24)
    assert result == []


async def test_gather_references_filed_keeps_items_without_timestamp():
    """If KS returns no timestamp, include the item rather than silently dropping it."""
    kc = _make_kc(
        {
            "reference": [
                {
                    "content_id": "no-ts",
                    "title": "Reference w/o timestamp",
                    "source_type": "reference",
                    "metadata": {"source_tag": "#research"},
                }
            ],
            "intelligence": [],
        }
    )
    act = BriefingActivities(db_pool=None, knowledge_connector=kc)
    env = ActivityEnvironment()
    result = await env.run(act.gather_references_filed, 24)
    assert len(result) == 1
    assert result[0]["content_id"] == "no-ts"
