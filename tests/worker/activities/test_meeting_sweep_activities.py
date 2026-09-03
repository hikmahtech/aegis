"""The sweep's two lookups: whose mail to sweep, and what is not filed yet.

Both are read-only and both fail closed. The sender list is DERIVED from the
`meeting` tag on the user's own triage overrides, so nothing here names a
vendor and a fresh install sweeps nobody.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from aegis.services.email_rules import SETTINGS_KEY
from aegis_worker.activities.meeting import MeetingActivities

pytestmark = pytest.mark.asyncio
PREFIX = "msw-test-"


class _BrokenPool:
    """Up as far as the caller can see, dead on use — a closed pool."""

    def acquire(self):
        raise RuntimeError("pool is closed")

    async def fetchrow(self, *args, **kwargs):
        raise RuntimeError("pool is closed")


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    async def _clean():
        await db_pool.execute(
            "DELETE FROM knowledge_content WHERE content_id LIKE $1", PREFIX + "%"
        )
        await db_pool.execute("DELETE FROM settings WHERE key = $1", SETTINGS_KEY)

    await _clean()
    yield db_pool
    await _clean()


async def _rules(pool, overrides: dict) -> None:
    await pool.execute(
        "INSERT INTO settings (key, value) VALUES ($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value = $2",
        SETTINGS_KEY,
        {"sender_overrides": overrides},
    )


async def _content(pool, source_type: str, message_id: str) -> str:
    cid = PREFIX + uuid.uuid4().hex[:12]
    await pool.execute(
        "INSERT INTO knowledge_content (content_id, url, title, source_type, metadata) "
        "VALUES ($1, $2, 'Standup', $3, $4)",
        cid,
        f"test://{cid}",
        source_type,
        {"message_id": message_id},
    )
    return cid


# ---------------------------------------------------------------------------
# meeting_sender_addresses
# ---------------------------------------------------------------------------


async def test_only_meeting_tagged_overrides_are_swept(pool):
    """The tag is the whole configuration surface: an override without it is
    somebody else's rule, and must not widen the sweep."""
    await _rules(
        pool,
        {
            "notes@vendor.example": {"category": "important_read", "tags": ["meeting"]},
            # `merge` lowercases tags and keys, and a domain key keeps its @.
            "@Notes-Vendor.example": {"category": "important_read", "tags": ["Meeting"]},
            "billing@bank.example": {"category": "important_read", "tags": ["financial"]},
            "plain@example.com": "informational",
        },
    )
    acts = MeetingActivities("c", "t", db_pool=pool)

    # Sorted, and the leading @ is stripped: a Gmail `from:` term wants
    # `notes-vendor.example`, not `@notes-vendor.example`.
    assert await acts.meeting_sender_addresses() == [
        "notes-vendor.example",
        "notes@vendor.example",
    ]


async def test_no_rules_row_sweeps_nobody(pool):
    """A fresh install has no row at all — the flow must be inert, not broad."""
    assert await MeetingActivities("c", "t", db_pool=pool).meeting_sender_addresses() == []


async def test_a_rules_row_with_no_meeting_tag_sweeps_nobody(pool):
    await _rules(pool, {"billing@bank.example": {"category": "useless", "tags": ["financial"]}})
    assert await MeetingActivities("c", "t", db_pool=pool).meeting_sender_addresses() == []


async def test_a_missing_or_broken_pool_never_raises(pool):
    assert await MeetingActivities("c", "t", db_pool=None).meeting_sender_addresses() == []
    assert await MeetingActivities("c", "t", db_pool=_BrokenPool()).meeting_sender_addresses() == []


# ---------------------------------------------------------------------------
# unstored_meeting_messages
# ---------------------------------------------------------------------------


async def test_only_ids_with_no_meeting_row_come_back_in_input_order(pool):
    await _content(pool, "meeting", "gm-stored")
    acts = MeetingActivities("c", "t", db_pool=pool)

    assert await acts.unstored_meeting_messages(
        ["gm-c", "gm-stored", "gm-a", "gm-b"]
    ) == ["gm-c", "gm-a", "gm-b"]


async def test_another_source_type_with_the_same_message_id_does_not_count_as_stored(pool):
    """MeetingNotesFlow files a `meeting_review` row under the same message, and
    the hourly path can file an `email` copy. Neither means the notes are in."""
    await _content(pool, "meeting_review", "gm-review-only")
    await _content(pool, "email", "gm-email-only")

    assert await MeetingActivities("c", "t", db_pool=pool).unstored_meeting_messages(
        ["gm-review-only", "gm-email-only"]
    ) == ["gm-review-only", "gm-email-only"]


async def test_empty_input_is_answered_without_touching_the_db():
    """No ids, no query — proven by handing it a pool that dies on use."""
    assert await MeetingActivities(
        "c", "t", db_pool=_BrokenPool()
    ).unstored_meeting_messages([]) == []


async def test_a_missing_or_broken_pool_files_nothing_rather_than_everything():
    """Fail CLOSED. "I cannot tell what is already stored" must not become
    "file all of it" — that would re-ingest and re-review every meeting in the
    window on every run. Skipping this run costs one cycle; the next recovers.
    """
    ids = ["gm-a", "gm-b"]
    assert await MeetingActivities("c", "t", db_pool=None).unstored_meeting_messages(ids) == []
    assert (
        await MeetingActivities("c", "t", db_pool=_BrokenPool()).unstored_meeting_messages(ids)
        == []
    )
