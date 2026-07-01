"""triage_accuracy feedback loop — record predictions, capture user corrections.

AEGIS re-fetches Gmail by `after:` timestamp, so an email it already actioned is
re-seen with whatever the user has since done to it. `record_triage_outcome`
logs the first prediction and, on a contradicting re-observation, records the
correction into the (previously unused) `triage_accuracy` table.
"""

from __future__ import annotations

import pytest
from aegis_worker.activities.gmail import GmailActivities, assess_triage_correction
from temporalio.testing import ActivityEnvironment

# ----- pure helper ----------------------------------------------------------


def test_assess_correction_unimportant_then_user_stars():
    assert assess_triage_correction("useless", ["INBOX", "STARRED"]) == "important"
    assert assess_triage_correction("informational", ["INBOX", "IMPORTANT"]) == "important"


def test_assess_correction_unimportant_consistent_is_none():
    # AEGIS marked it read (no IMPORTANT/STARRED) and the user left it → no signal.
    assert assess_triage_correction("useless", ["INBOX"]) is None


def test_assess_correction_important_then_user_demotes():
    # AEGIS labelled IMPORTANT + kept unread; user read it and dropped importance.
    assert assess_triage_correction("important_read", ["INBOX"]) == "unimportant"


def test_assess_correction_important_consistent_is_none():
    # Still IMPORTANT (or still unread) → user hasn't demoted → no signal.
    assert assess_triage_correction("important_action", ["INBOX", "IMPORTANT"]) is None
    assert assess_triage_correction("important_read", ["INBOX", "UNREAD"]) is None


# ----- activity (real Postgres) ---------------------------------------------


@pytest.mark.asyncio
async def test_record_triage_outcome_first_sight_inserts(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM triage_accuracy WHERE email_id='E_FB1'")
    act = GmailActivities(gmail_credentials_file="x", gmail_token_dir="x", db_pool=db_pool)
    env = ActivityEnvironment()
    res = await env.run(act.record_triage_outcome, "E_FB1", "useless", ["INBOX"])
    assert res["outcome"] == "predicted"
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT predicted, actual FROM triage_accuracy WHERE email_id='E_FB1'"
        )
    assert row["predicted"] == "useless"
    assert row["actual"] is None
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM triage_accuracy WHERE email_id='E_FB1'")


@pytest.mark.asyncio
async def test_record_triage_outcome_resight_captures_correction(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM triage_accuracy WHERE email_id='E_FB2'")
    act = GmailActivities(gmail_credentials_file="x", gmail_token_dir="x", db_pool=db_pool)
    env = ActivityEnvironment()
    # First sight: AEGIS said useless.
    await env.run(act.record_triage_outcome, "E_FB2", "useless", ["INBOX"])
    # Re-sight: user has STARRED it → correction toward important.
    res = await env.run(act.record_triage_outcome, "E_FB2", "useless", ["INBOX", "STARRED"])
    assert res["outcome"] == "corrected"
    assert res["actual"] == "important"
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT predicted, actual, corrected_by FROM triage_accuracy WHERE email_id='E_FB2'"
        )
    assert (row["predicted"], row["actual"], row["corrected_by"]) == (
        "useless",
        "important",
        "user_gmail",
    )
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM triage_accuracy WHERE email_id='E_FB2'")


@pytest.mark.asyncio
async def test_record_triage_outcome_resight_consistent_no_update(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM triage_accuracy WHERE email_id='E_FB3'")
    act = GmailActivities(gmail_credentials_file="x", gmail_token_dir="x", db_pool=db_pool)
    env = ActivityEnvironment()
    await env.run(act.record_triage_outcome, "E_FB3", "useless", ["INBOX"])
    res = await env.run(act.record_triage_outcome, "E_FB3", "useless", ["INBOX"])
    assert res["outcome"] == "consistent"
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT actual FROM triage_accuracy WHERE email_id='E_FB3'")
    assert row["actual"] is None
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM triage_accuracy WHERE email_id='E_FB3'")


@pytest.mark.asyncio
async def test_record_triage_outcome_no_pool_is_noop():
    act = GmailActivities(gmail_credentials_file="x", gmail_token_dir="x", db_pool=None)
    env = ActivityEnvironment()
    res = await env.run(act.record_triage_outcome, "E_FB4", "useless", ["INBOX"])
    assert res == {"recorded": False}
