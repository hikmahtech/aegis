"""triage_accuracy feedback loop — record predictions, capture user corrections.

`record_triage_outcome` logs each prediction; `recheck_triage_outcomes` (#74)
actively re-reads the emails' current Gmail labels (the ingest fetch never
re-observes an actioned email), records contradictions as corrections, and —
once the 7d window closes — implicitly confirms checked-but-uncorrected rows
so accuracy is computable.
"""

from __future__ import annotations

import pytest
from aegis_worker.activities import gmail as gmail_mod
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


# ----- recheck_triage_outcomes (#74) -----------------------------------------


class _FakeGmail:
    """Just enough of the googleapiclient chain for messages().get().execute()."""

    def __init__(self, labels_by_id: dict):
        self._labels = labels_by_id
        self._current = ""

    def users(self):
        return self

    def messages(self):
        return self

    def get(self, userId: str, id: str, format: str):  # noqa: A002, N803 — API shape
        self._current = id
        return self

    def execute(self):
        labels = self._labels.get(self._current)
        if labels is None:
            raise RuntimeError("404 not found")
        return {"labelIds": labels}


async def _seed_prediction(db_pool, email_id, predicted, age, checked_age=None):
    await db_pool.execute(
        "INSERT INTO triage_accuracy (email_id, predicted, created_at, last_checked_at) "
        "VALUES ($1, $2, now() - ($3::text)::interval, "
        "        CASE WHEN $4::text IS NULL THEN NULL ELSE now() - ($4::text)::interval END)",
        email_id,
        predicted,
        age,
        checked_age,
    )


async def _wipe(db_pool):
    await db_pool.execute("DELETE FROM triage_accuracy WHERE email_id LIKE 'E_RC%'")


async def test_recheck_corrects_from_current_labels(db_pool, monkeypatch):
    await _wipe(db_pool)
    await _seed_prediction(db_pool, "E_RC1", "useless", "2 hours")
    monkeypatch.setattr(
        gmail_mod, "_build_gmail_service", lambda *a: _FakeGmail({"E_RC1": ["INBOX", "STARRED"]})
    )
    act = GmailActivities(gmail_credentials_file="x", gmail_token_dir="x", db_pool=db_pool)
    res = await ActivityEnvironment().run(act.recheck_triage_outcomes, "acct")
    assert res == {"checked": 1, "corrected": 1, "confirmed": 0}
    row = await db_pool.fetchrow(
        "SELECT actual, corrected_by, last_checked_at FROM triage_accuracy "
        "WHERE email_id='E_RC1'"
    )
    assert row["actual"] == "important"
    assert row["corrected_by"] == "user_gmail"
    assert row["last_checked_at"] is not None
    await _wipe(db_pool)


async def test_recheck_consistent_stamps_only(db_pool, monkeypatch):
    await _wipe(db_pool)
    await _seed_prediction(db_pool, "E_RC2", "useless", "2 hours")
    monkeypatch.setattr(
        gmail_mod, "_build_gmail_service", lambda *a: _FakeGmail({"E_RC2": ["INBOX"]})
    )
    act = GmailActivities(gmail_credentials_file="x", gmail_token_dir="x", db_pool=db_pool)
    res = await ActivityEnvironment().run(act.recheck_triage_outcomes, "acct")
    assert res == {"checked": 1, "corrected": 0, "confirmed": 0}
    row = await db_pool.fetchrow(
        "SELECT actual, last_checked_at FROM triage_accuracy WHERE email_id='E_RC2'"
    )
    assert row["actual"] is None
    assert row["last_checked_at"] is not None
    await _wipe(db_pool)


async def test_recheck_implicit_confirms_only_checked_rows(db_pool):
    await _wipe(db_pool)
    # Past the 7d window: one row was observed at least once, one never was.
    await _seed_prediction(db_pool, "E_RC3", "useless", "8 days", checked_age="5 days")
    await _seed_prediction(db_pool, "E_RC4", "useless", "8 days")
    act = GmailActivities(gmail_credentials_file="x", gmail_token_dir="x", db_pool=db_pool)
    res = await ActivityEnvironment().run(act.recheck_triage_outcomes, "acct")
    assert res == {"checked": 0, "corrected": 0, "confirmed": 1}
    rows = {
        r["email_id"]: r
        for r in await db_pool.fetch(
            "SELECT email_id, actual, corrected_by FROM triage_accuracy "
            "WHERE email_id LIKE 'E_RC%'"
        )
    }
    assert rows["E_RC3"]["actual"] == "useless"
    assert rows["E_RC3"]["corrected_by"] == "implicit"
    assert rows["E_RC4"]["actual"] is None  # never observed — stays honest NULL
    await _wipe(db_pool)


async def test_recheck_unobservable_message_skipped(db_pool, monkeypatch):
    await _wipe(db_pool)
    await _seed_prediction(db_pool, "E_RC5", "useless", "2 hours")
    monkeypatch.setattr(gmail_mod, "_build_gmail_service", lambda *a: _FakeGmail({}))
    act = GmailActivities(gmail_credentials_file="x", gmail_token_dir="x", db_pool=db_pool)
    res = await ActivityEnvironment().run(act.recheck_triage_outcomes, "acct")
    assert res == {"checked": 0, "corrected": 0, "confirmed": 0}
    row = await db_pool.fetchrow(
        "SELECT actual, last_checked_at FROM triage_accuracy WHERE email_id='E_RC5'"
    )
    assert row["actual"] is None
    assert row["last_checked_at"] is None  # keeps queue-front priority
    await _wipe(db_pool)


async def test_recheck_service_down_never_raises(db_pool, monkeypatch):
    await _wipe(db_pool)
    await _seed_prediction(db_pool, "E_RC6", "useless", "2 hours")

    def _boom(*a):
        raise RuntimeError("token refresh failed")

    monkeypatch.setattr(gmail_mod, "_build_gmail_service", _boom)
    act = GmailActivities(gmail_credentials_file="x", gmail_token_dir="x", db_pool=db_pool)
    res = await ActivityEnvironment().run(act.recheck_triage_outcomes, "acct")
    assert res == {"checked": 0, "corrected": 0, "confirmed": 0}
    await _wipe(db_pool)
