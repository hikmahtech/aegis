"""Two briefing content bugs, both of which shipped a "delivered" briefing.

1. `gather_briefing_changes` fetched intelligence through the healthy
   `list_content_items` path and then dropped it with
   `if r.get("source_type") != "reference": continue`, on the comment's claim
   that "intelligence is already covered by `intel` (sig>=4)". `intel` comes
   from a VECTOR search that pgvector caps at `hnsw.ef_search` candidates, so
   it delivered ~nothing: 162 intelligence items ingested over 26 days in
   prod, exactly 1 reached a briefing.

2. `important_read` — 60% of triaged mail — is filed to the knowledge store
   and marked read in the same step, so the owner never sees it.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from aegis_worker.activities.briefing import BriefingActivities
from temporalio.testing import ActivityEnvironment


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _kc(items_by_source: dict[str, list[dict]]) -> AsyncMock:
    kc = AsyncMock()

    async def _list(limit: int = 200, source_type: str | None = None):
        return list(items_by_source.get(source_type or "", []))

    kc.list_content_items = AsyncMock(side_effect=_list)
    # The starved path: whatever the vector search would have returned.
    kc.search = AsyncMock(return_value=[])
    return kc


def _email(cid: str, title: str, *, category="important_read", age_h=2, sender="a@b.com"):
    return {
        "content_id": cid,
        "title": title,
        "source_type": "email",
        "metadata": {"category": category, "sender": sender, "lane": "own"},
        "ingested_at": _iso(datetime.now(UTC) - timedelta(hours=age_h)),
    }


# ---------------------------------------------------------------- email digest


@pytest.mark.asyncio
async def test_email_digest_keeps_the_two_tiers_the_owner_never_sees():
    kc = _kc(
        {
            "email": [
                _email("e1", "Red Hat Developer Weekly"),
                _email("e2", "Payment declined", category="important_action"),
                _email("e3", "LinkedIn invitations", category="informational"),
                _email("e4", "Buy now", category="useless"),
            ]
        }
    )
    out = await BriefingActivities(knowledge_connector=kc).gather_email_digest(hours=24)

    titles = [m["title"] for m in out]
    assert titles == ["Red Hat Developer Weekly", "Payment declined"]
    # informational is stored nowhere and is the low-value tier — including it
    # would dilute the digest, which is the whole point of having one.
    assert "LinkedIn invitations" not in titles


@pytest.mark.asyncio
async def test_email_digest_drops_mail_older_than_the_window():
    kc = _kc({"email": [_email("old", "Yesterday's news", age_h=48)]})
    out = await BriefingActivities(knowledge_connector=kc).gather_email_digest(hours=24)
    assert out == []


@pytest.mark.asyncio
async def test_email_digest_survives_a_dead_knowledge_store():
    kc = AsyncMock()
    kc.list_content_items = AsyncMock(side_effect=RuntimeError("KS down"))
    out = await BriefingActivities(knowledge_connector=kc).gather_email_digest()
    assert out == [], "a failing digest must degrade to empty, never kill the briefing"


@pytest.mark.asyncio
async def test_repeated_ci_subjects_collapse_to_one_line(db_pool):
    """CI mail repeats the same subject for the same commit several times a
    day and is ~40% of this tier by volume. Without collapsing, one noisy repo
    fills the 12-item digest on its own."""
    noisy = [
        _email(f"ci{i}", "[hikmahtech/homelab-gitops] Run failed: Ansible Validation")
        for i in range(6)
    ]
    kc = _kc({"email": [*noisy, _email("real", "Stock SIP: Instalment due in 3 days")]})
    acts = BriefingActivities(knowledge_connector=kc, db_pool=db_pool)

    changes = await ActivityEnvironment().run(acts.gather_briefing_changes)

    titles = [m["title"] for m in changes["emails"]]
    assert titles.count("[hikmahtech/homelab-gitops] Run failed: Ansible Validation") == 1
    assert "Stock SIP: Instalment due in 3 days" in titles


# ------------------------------------------------------- intelligence recovery


@pytest.mark.asyncio
async def test_intelligence_reaches_the_briefing_when_the_vector_search_returns_nothing(db_pool):
    """The headline regression. `search` (the `intel` bundle) is empty, exactly
    as in prod — the items must still arrive via the listing path."""
    now = datetime.now(UTC)
    kc = _kc(
        {
            "reference": [
                {
                    "content_id": "r1",
                    "title": "Hungary sinks barges into the Danube",
                    "source_type": "reference",
                    "ingested_at": _iso(now - timedelta(hours=3)),
                }
            ],
            "intelligence": [
                {
                    "content_id": "i1",
                    "title": "China Wants to Shape What the World's A.I. Knows",
                    "source_type": "intelligence",
                    "metadata": {"significance": 5},
                    "ingested_at": _iso(now - timedelta(hours=3)),
                }
            ],
        }
    )
    acts = BriefingActivities(knowledge_connector=kc, db_pool=db_pool)

    changes = await ActivityEnvironment().run(acts.gather_briefing_changes)

    collected_titles = [c["title"] for c in changes["collected"]]
    assert "China Wants to Shape What the World's A.I. Knows" in collected_titles, (
        "intelligence fetched through the healthy listing path was discarded"
    )
    assert "Hungary sinks barges into the Danube" in collected_titles
    assert changes["quiet"] is False


@pytest.mark.asyncio
async def test_an_item_the_vector_search_did_return_is_not_listed_twice(db_pool):
    """`seen_intel` and `seen_ref` are separate sets, so without an explicit
    cross-check the same item lands in both `intel` and `collected`."""
    now = datetime.now(UTC)
    item = {
        "content_id": "dup1",
        "title": "Both paths found me",
        "source_type": "intelligence",
        "metadata": {"significance": 5},
        "ingested_at": _iso(now - timedelta(hours=1)),
    }
    kc = _kc({"intelligence": [item], "reference": []})
    kc.search = AsyncMock(return_value=[item])  # vector search DOES return it
    acts = BriefingActivities(knowledge_connector=kc, db_pool=db_pool)

    changes = await ActivityEnvironment().run(acts.gather_briefing_changes)

    assert [i["title"] for i in changes["intel"]] == ["Both paths found me"]
    assert [c["title"] for c in changes["collected"]] == []
