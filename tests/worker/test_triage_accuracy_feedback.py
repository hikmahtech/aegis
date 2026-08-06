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
    res = await env.run(act.record_triage_outcome, "E_FB1", "useless", ["INBOX"], "acct-b")
    assert res["outcome"] == "predicted"
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT predicted, actual, account_label FROM triage_accuracy WHERE email_id='E_FB1'"
        )
    assert row["predicted"] == "useless"
    assert row["actual"] is None
    # (#260) The owning mailbox is captured here or nowhere — this is the only
    # INSERT into the table, and the recheck cannot re-derive it later.
    assert row["account_label"] == "acct-b"
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

    def __init__(
        self,
        labels_by_id: dict,
        subjects_by_id: dict | None = None,
        senders_by_id: dict | None = None,
    ):
        self._labels = labels_by_id
        self._subjects = subjects_by_id or {}
        self._senders = senders_by_id or {}
        self._current = ""
        # (#102) Records the metadataHeaders each get() asked for, so a test
        # can prove the From header is actually requested — a header the API
        # is never asked for comes back empty no matter what the fake holds.
        self.requested_headers: list[list[str]] = []
        # (#260) Records which message ids this account was asked for, so a
        # test can prove another account's rows aren't burning Gmail quota.
        self.requested_ids: list[str] = []

    def users(self):
        return self

    def messages(self):
        return self

    def get(
        self,
        userId: str,  # noqa: N803 — API shape
        id: str,  # noqa: A002 — API shape
        format: str,
        metadataHeaders: list[str] | None = None,  # noqa: N803 — API shape
    ):
        self._current = id
        self.requested_ids.append(id)
        self.requested_headers.append(list(metadataHeaders or []))
        return self

    def execute(self):
        labels = self._labels.get(self._current)
        if labels is None:
            raise RuntimeError("404 not found")
        requested = set(self.requested_headers[-1]) if self.requested_headers else set()
        headers = []
        if "Subject" in requested:
            headers.append({"name": "Subject", "value": self._subjects.get(self._current, "")})
        if "From" in requested:
            headers.append({"name": "From", "value": self._senders.get(self._current, "")})
        return {"labelIds": labels, "payload": {"headers": headers}}


async def _seed_prediction(db_pool, email_id, predicted, age, checked_age=None, account=None):
    await db_pool.execute(
        "INSERT INTO triage_accuracy "
        "(email_id, predicted, created_at, last_checked_at, account_label) "
        "VALUES ($1, $2, now() - ($3::text)::interval, "
        "        CASE WHEN $4::text IS NULL THEN NULL ELSE now() - ($4::text)::interval END, $5)",
        email_id,
        predicted,
        age,
        checked_age,
        account,
    )


async def _wipe(db_pool):
    await db_pool.execute("DELETE FROM triage_accuracy WHERE email_id LIKE 'E_RC%'")
    await db_pool.execute(
        "DELETE FROM agent_memory WHERE agent_id='sebas' AND content LIKE '%[gmail:E_RC%'"
    )
    await db_pool.execute(
        "DELETE FROM triage_state WHERE email_addr LIKE 'zz102-%' OR email_addr LIKE 'zz260-%'"
    )


async def test_recheck_corrects_from_current_labels(db_pool, monkeypatch):
    await _wipe(db_pool)
    await _seed_prediction(db_pool, "E_RC1", "useless", "2 hours")
    monkeypatch.setattr(
        gmail_mod,
        "_build_gmail_service",
        lambda *a: _FakeGmail(
            {"E_RC1": ["INBOX", "STARRED"]},
            {"E_RC1": "You've been added to a board"},
            {"E_RC1": "Board Bot <zz102-board@example.com>"},
        ),
    )
    act = GmailActivities(gmail_credentials_file="x", gmail_token_dir="x", db_pool=db_pool)
    res = await ActivityEnvironment().run(act.recheck_triage_outcomes, "acct")
    assert res == {
        "checked": 1,
        "corrected": 1,
        "confirmed": 0,
        "memories_written": 1,
        "senders_relearned": 1,
        "disposition_corrected": 0,
    }
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
    assert res == {
        "checked": 1,
        "corrected": 0,
        "confirmed": 0,
        "memories_written": 0,
        "senders_relearned": 0,
        "disposition_corrected": 0,
    }
    row = await db_pool.fetchrow(
        "SELECT actual, last_checked_at FROM triage_accuracy WHERE email_id='E_RC2'"
    )
    assert row["actual"] is None
    assert row["last_checked_at"] is not None
    await _wipe(db_pool)


async def test_recheck_implicit_confirms_checked_and_never_checked_rows(db_pool):
    """(#115) Past the 7d window, silence is agreement whether or not the row
    was ever actively checked — a never-checked row must not stay stuck with
    actual NULL forever just because the recheck loop never got to it."""
    await _wipe(db_pool)
    await _seed_prediction(db_pool, "E_RC3", "useless", "8 days", checked_age="5 days")
    await _seed_prediction(db_pool, "E_RC4", "useless", "8 days")  # never checked
    act = GmailActivities(gmail_credentials_file="x", gmail_token_dir="x", db_pool=db_pool)
    res = await ActivityEnvironment().run(act.recheck_triage_outcomes, "acct")
    assert res == {
        "checked": 0,
        "corrected": 0,
        "confirmed": 2,
        "memories_written": 0,
        "senders_relearned": 0,
        "disposition_corrected": 0,
    }
    rows = {
        r["email_id"]: r
        for r in await db_pool.fetch(
            "SELECT email_id, actual, corrected_by FROM triage_accuracy "
            "WHERE email_id LIKE 'E_RC%'"
        )
    }
    assert rows["E_RC3"]["actual"] == "useless"
    assert rows["E_RC3"]["corrected_by"] == "implicit"
    assert rows["E_RC4"]["actual"] == "useless"  # (#115) no longer stuck at NULL
    assert rows["E_RC4"]["corrected_by"] == "implicit"
    await _wipe(db_pool)


async def test_recheck_unobservable_message_stamps_last_checked_at(db_pool, monkeypatch):
    """(#115) An unresolvable row (deleted mail / another account's message)
    must still get last_checked_at stamped, or it permanently wins
    queue-front priority (NULLS FIRST) on every future call and starves
    genuinely-resolvable rows behind it."""
    await _wipe(db_pool)
    await _seed_prediction(db_pool, "E_RC5", "useless", "2 hours")
    monkeypatch.setattr(gmail_mod, "_build_gmail_service", lambda *a: _FakeGmail({}))
    act = GmailActivities(gmail_credentials_file="x", gmail_token_dir="x", db_pool=db_pool)
    res = await ActivityEnvironment().run(act.recheck_triage_outcomes, "acct")
    assert res == {
        "checked": 0,
        "corrected": 0,
        "confirmed": 0,
        "memories_written": 0,
        "senders_relearned": 0,
        "disposition_corrected": 0,
    }
    row = await db_pool.fetchrow(
        "SELECT actual, last_checked_at FROM triage_accuracy WHERE email_id='E_RC5'"
    )
    assert row["actual"] is None
    assert row["last_checked_at"] is not None  # no longer camps at queue-front forever
    await _wipe(db_pool)


async def test_recheck_scopes_rows_to_their_owning_account(db_pool, monkeypatch):
    """(#260) A correction on the SECOND account must be detected.

    Predictions used to be selected account-agnostically and resolved with one
    account's token, so account #1 404'd on account #2's rows, recorded them as
    "unobservable" and stamped last_checked_at — pushing them behind the
    NULLS-FIRST queue before account #2 (which runs after it, in the same flow)
    ever looked. The user's IMPORTANT label was structurally undetectable and
    the row later aged into corrected_by='implicit', i.e. "AEGIS was right".

    The batch limit is what makes the starvation observable, exactly as in prod
    (LIMIT 50 with more unscored rows than that), so this runs with limit=1 and
    seeds acct-b's row OLDER so it sorts to the front of acct-a's batch.
    """
    await _wipe(db_pool)
    await _seed_prediction(db_pool, "E_RC8", "informational", "3 hours", account="acct-b")
    await _seed_prediction(db_pool, "E_RC7", "useless", "2 hours", account="acct-a")
    fakes = {
        "acct-a": _FakeGmail({"E_RC7": ["INBOX"]}),
        "acct-b": _FakeGmail(
            {"E_RC8": ["INBOX", "IMPORTANT"]},
            {"E_RC8": "Invoice overdue"},
            {"E_RC8": "Billing <zz260-billing@example.com>"},
        ),
    }
    act = GmailActivities(gmail_credentials_file="x", gmail_token_dir="x", db_pool=db_pool)

    # Accounts run in sequence within one GmailIngestFlow run.
    for account in ("acct-a", "acct-b"):
        monkeypatch.setattr(gmail_mod, "_build_gmail_service", lambda *a, _f=fakes[account]: _f)
        res = await ActivityEnvironment().run(act.recheck_triage_outcomes, account, 1)

    # acct-b's pass — the one that owns E_RC8 — recorded the human correction.
    assert res["corrected"] == 1
    row = await db_pool.fetchrow(
        "SELECT actual, corrected_by FROM triage_accuracy WHERE email_id='E_RC8'"
    )
    assert row["actual"] == "important"
    assert row["corrected_by"] == "user_gmail"
    # acct-a never spent a doomed messages.get on a mailbox it cannot read.
    assert fakes["acct-a"].requested_ids == ["E_RC7"]
    await _wipe(db_pool)


async def test_recheck_service_down_never_raises(db_pool, monkeypatch):
    await _wipe(db_pool)
    await _seed_prediction(db_pool, "E_RC6", "useless", "2 hours")

    def _boom(*a):
        raise RuntimeError("token refresh failed")

    monkeypatch.setattr(gmail_mod, "_build_gmail_service", _boom)
    act = GmailActivities(gmail_credentials_file="x", gmail_token_dir="x", db_pool=db_pool)
    res = await ActivityEnvironment().run(act.recheck_triage_outcomes, "acct")
    assert res == {
        "checked": 0,
        "corrected": 0,
        "confirmed": 0,
        "memories_written": 0,
        "senders_relearned": 0,
        "disposition_corrected": 0,
    }
    await _wipe(db_pool)


# ----- agent_memory writes on a real triage correction (#116) ----------------


async def test_recheck_correction_writes_agent_memory_once(db_pool, monkeypatch):
    """A detected correction (actual != predicted) writes exactly one
    agent_memory row — and re-running the recheck over the same,
    already-corrected email_id must not write a duplicate."""
    await _wipe(db_pool)
    await _seed_prediction(db_pool, "E_RC7", "useless", "2 hours")
    monkeypatch.setattr(
        gmail_mod,
        "_build_gmail_service",
        lambda *a: _FakeGmail({"E_RC7": ["INBOX", "STARRED"]}, {"E_RC7": "Re: your order"}),
    )
    act = GmailActivities(gmail_credentials_file="x", gmail_token_dir="x", db_pool=db_pool)

    res = await ActivityEnvironment().run(act.recheck_triage_outcomes, "acct")
    assert res["memories_written"] == 1
    rows = await db_pool.fetch(
        "SELECT content, importance, source FROM agent_memory "
        "WHERE agent_id='sebas' AND content LIKE '%[gmail:E_RC7]%'"
    )
    assert len(rows) == 1
    assert "Re: your order" in rows[0]["content"]
    assert "predicted useless" in rows[0]["content"]
    assert "actually important" in rows[0]["content"]
    assert rows[0]["source"] == "gmail_triage_correction"

    # Re-run: the row is no longer actual IS NULL, so recheck_triage_outcomes
    # won't even re-select it — but the idempotency guard is what protects
    # against a flip-flop re-correction of the same email_id, exercised
    # directly here.
    from aegis.services.memory import record_gmail_triage_correction

    wrote_again = await record_gmail_triage_correction(
        db_pool, "sebas", "E_RC7", "Re: your order", "useless", "important"
    )
    assert wrote_again is False
    rows_after = await db_pool.fetch(
        "SELECT id FROM agent_memory WHERE agent_id='sebas' AND content LIKE '%[gmail:E_RC7]%'"
    )
    assert len(rows_after) == 1
    await _wipe(db_pool)


# ----- (#102) the correction signal must be genuine, and must teach ----------


class _StatefulFakeGmail:
    """A fake whose messages().modify() actually mutates the stored label set,
    so a test can chain the real `apply_label` into the real
    `assess_triage_correction` over one message — the exact production chain
    that manufactured the phantom corrections."""

    def __init__(self, labels: list[str]):
        self.labels = list(labels)

    def users(self):
        return self

    def messages(self):
        return self

    def modify(
        self,
        userId: str,  # noqa: N803 — API shape
        id: str,  # noqa: A002 — API shape
        body: dict,
    ):
        for lid in body.get("removeLabelIds") or []:
            if lid in self.labels:
                self.labels.remove(lid)
        for lid in body.get("addLabelIds") or []:
            if lid not in self.labels:
                self.labels.append(lid)
        return self

    def execute(self):
        return {"id": "M1"}


@pytest.mark.asyncio
async def test_auto_important_no_longer_reads_as_a_user_correction(monkeypatch):
    """(#102) Gmail auto-applies IMPORTANT at delivery. Before this fix the
    useless/informational verdict removed only UNREAD, so that marker survived
    and the recheck loop read it as "the user elevated this" — which is why
    prod recorded 75 corrections, ALL unimportant→important and none the other
    way, over subject lines like "Skip the scrolling. Watch this instead."
    (logged twice, once per duplicate campaign send).

    Asserted in three independent steps so none can mask another: the
    delivered state really is the polluting one, the verdict really strips
    IMPORTANT on the wire, and only then that assess sees no correction.
    """
    delivered = ["INBOX", "UNREAD", "IMPORTANT", "CATEGORY_PROMOTIONS"]
    # 1. Baseline — this label set is exactly what used to be misread.
    assert assess_triage_correction("useless", delivered) == "important"

    svc = _StatefulFakeGmail(delivered)
    monkeypatch.setattr(gmail_mod, "_build_gmail_service", lambda *a: svc)
    act = GmailActivities(gmail_credentials_file="x", gmail_token_dir="x")
    res = await ActivityEnvironment().run(act.apply_label, "acct", "M1", "READ")
    assert res["ok"] is True

    # 2. The verdict removed IMPORTANT (and UNREAD) and nothing else.
    assert svc.labels == ["INBOX", "CATEGORY_PROMOTIONS"]
    # 3. With no human involved, there is now no correction to record.
    assert assess_triage_correction("useless", svc.labels) is None
    # ...but a human who re-elevates it still registers — the signal is
    # narrowed to real input, not switched off.
    assert assess_triage_correction("useless", [*svc.labels, "IMPORTANT"]) == "important"


def _meta_of(row) -> dict:
    """Decode a triage_state metadata cell without going through the module
    under test."""
    import json

    meta = row["metadata"]
    return json.loads(meta) if isinstance(meta, str) else dict(meta)


@pytest.mark.asyncio
async def test_correction_relearns_sender_and_flips_a_poisoned_cache(db_pool, monkeypatch):
    """(#102) Corrections used to be write-only: recorded in triage_accuracy
    and agent_memory, never fed back into `triage_state`. Because the classify
    cascade short-circuits the LLM entirely at n>=3/conf>=0.75, a sender cached
    wrong (e.g. seeded 'useless' by the gmail_promo shortcut) stayed wrong
    forever — no amount of user correction could reach it.

    Walks the disagreement arithmetic _triage_upsert already owns:
    conf 0.9 -> 0.6 -> (<=0.3) flip.
    """
    await _wipe(db_pool)
    sender = "zz102-promo@example.com"
    await db_pool.execute(
        "INSERT INTO triage_state (email_addr, state, metadata, updated_at) "
        "VALUES ($1, 'useless', $2, now())",
        sender,
        {"n": 5, "confidence": 0.9, "category": "useless"},
    )
    # No llm_client on purpose: a cache MISS falls through to source='fallback',
    # so 'cache' below cannot be an accident of the default return shape.
    act = GmailActivities(gmail_credentials_file="x", gmail_token_dir="x", db_pool=db_pool)

    before = await ActivityEnvironment().run(
        act.classify_email, {"id": "M0", "sender": f"Promo <{sender}>", "labels": []}, ""
    )
    assert (before["source"], before["category"]) == ("cache", "useless")

    async def _correct(email_id: str) -> dict:
        await _seed_prediction(db_pool, email_id, "useless", "2 hours")
        monkeypatch.setattr(
            gmail_mod,
            "_build_gmail_service",
            lambda *a, _id=email_id: _FakeGmail(
                {_id: ["INBOX", "STARRED"]},
                {_id: "Re: contract"},
                {_id: f"Promo <{sender}>"},
            ),
        )
        return await ActivityEnvironment().run(act.recheck_triage_outcomes, "acct")

    res1 = await _correct("E_RC10")
    assert (res1["corrected"], res1["senders_relearned"]) == (1, 1)
    row = await db_pool.fetchrow(
        "SELECT state, metadata FROM triage_state WHERE email_addr=$1", sender
    )
    # One disagreement: confidence drops 0.3, category not yet flipped.
    assert (row["state"], _meta_of(row)["confidence"]) == ("useless", 0.6)

    res2 = await _correct("E_RC11")
    assert (res2["corrected"], res2["senders_relearned"]) == (1, 1)
    row = await db_pool.fetchrow(
        "SELECT state, metadata FROM triage_state WHERE email_addr=$1", sender
    )
    # Second disagreement bottoms out (0.6 - 0.3 <= 0.3) and flips the cached
    # verdict to the conservative important tier, reset to 0.6.
    assert (row["state"], _meta_of(row)["confidence"]) == ("important_read", 0.6)

    # And the poisoned short-circuit is gone: the sender no longer forces
    # 'useless' without the LLM ever being consulted.
    after = await ActivityEnvironment().run(
        act.classify_email, {"id": "M9", "sender": f"Promo <{sender}>", "labels": []}, ""
    )
    assert after["source"] != "cache"
    await _wipe(db_pool)


@pytest.mark.asyncio
async def test_recheck_requests_the_from_header_it_relearns_on(db_pool, monkeypatch):
    """(#102) triage_accuracy stores no sender, so the relearn step depends on
    From being added to the metadataHeaders of the recheck fetch. A header the
    Gmail API is never asked for comes back empty regardless of what the
    mailbox holds, silently turning the relearn into a no-op — assert the
    request, not just the outcome."""
    await _wipe(db_pool)
    await _seed_prediction(db_pool, "E_RC12", "useless", "2 hours")
    fake = _FakeGmail(
        {"E_RC12": ["INBOX", "STARRED"]},
        {"E_RC12": "Re: invoice"},
        {"E_RC12": "Acct <zz102-acct@example.com>"},
    )
    monkeypatch.setattr(gmail_mod, "_build_gmail_service", lambda *a: fake)
    act = GmailActivities(gmail_credentials_file="x", gmail_token_dir="x", db_pool=db_pool)
    res = await ActivityEnvironment().run(act.recheck_triage_outcomes, "acct")
    assert fake.requested_headers == [["Subject", "From"]]
    assert res["senders_relearned"] == 1
    row = await db_pool.fetchrow(
        "SELECT state FROM triage_state WHERE email_addr='zz102-acct@example.com'"
    )
    assert row is not None, "correction must create the sender's triage_state row"
    assert row["state"] == "important_read"
    await _wipe(db_pool)
