"""The 2026-08 email-triage noise fixes.

Three defects, all confirmed against production data before the fix:

1. `important_read` — 68% of all triaged mail — was labelled IMPORTANT and left
   UNREAD forever, with nothing anywhere that ever cleared it. The unread count
   could only grow.
2. Pure-notification senders (no-reply@accounts.google.com,
   security-noreply@linkedin.com, …) had accumulated `important_action` in the
   per-sender cache, and the cache short-circuits the LLM — so "Security Alert:
   Your one-time sign in code is 429718" kept minting Todoist tasks and a
   prompt fix alone could never have stopped it.
3. The feedback loop could only ratchet toward "important": 76 corrections in
   prod, 100% unimportant→important, zero the other way, because the
   Gmail-label detector requires IMPORTANT to be absent and AEGIS stamps it
   itself. The user's disposal of the captured Todoist task is the missing
   negative signal.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from aegis_worker.activities.gmail import (
    GmailActivities,
    _sender_from_description,
    cap_notification_category,
)
from temporalio.testing import ActivityEnvironment


class _CountingLlm:
    def __init__(self, response: str = "{}"):
        self.response = response
        self.calls = 0

    async def think(self, **kwargs):
        self.calls += 1
        return {"response": self.response, "model": "kimi-k2.5"}


class _FakePool:
    """Minimal asyncpg-shaped pool serving one `settings.email_triage_rules` row."""

    def __init__(self, rules: dict | None = None):
        self._rules = rules

    async def fetchrow(self, query: str, *args):
        if "email_triage_rules" in str(args):
            return {"value": self._rules} if self._rules is not None else None
        return None


def _make(llm=None, lookup=None, rules: dict | None = None) -> GmailActivities:
    g = GmailActivities(
        gmail_credentials_file="/tmp/x.json",
        gmail_token_dir="/tmp",
        llm_client=llm,
        db_pool=_FakePool(rules),
    )
    g._triage_lookup = AsyncMock(return_value=lookup)
    g._triage_upsert = AsyncMock(return_value=None)
    return g


# --------------------------------------------------------------------------
# Defect 2 — the notification cap
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cached_important_action_notification_is_capped_before_routing():
    """The exact production failure: a sender the cache has learned as
    important_action (n=6, conf=0.9) sending a sign-in alert. The cache path
    never reaches the LLM, so the cap has to sit downstream of it — this fails
    if the cap is only applied to LLM verdicts.
    """
    llm = _CountingLlm()
    g = _make(llm=llm, lookup={"category": "important_action", "n": 6, "confidence": 0.9, "tags": []})
    msg = {
        "id": "m",
        "sender": "Google <no-reply@accounts.google.com>",
        "subject": "Security Alert: Your one-time sign in code is 429718.",
        "snippet": "",
        "labels": [],
    }
    res = await ActivityEnvironment().run(g.classify_email, msg, "")
    assert res["category"] == "important_read"  # NOT important_action
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_llm_important_action_notification_is_capped_before_it_teaches_the_cache():
    """The capped verdict — not the raw LLM one — must be what reaches
    `_triage_upsert`, or a notification sender keeps accruing important_action
    reputation and starts short-circuiting the LLM straight into a task.
    """
    llm = _CountingLlm(
        json.dumps({"category": "important_action", "confidence": 0.95, "tags": ["security"]})
    )
    g = _make(llm=llm, lookup=None)
    msg = {
        "id": "m",
        "sender": "security-noreply@linkedin.com",
        "subject": "Account Activity: New Sign-In detected for your account.",
        "snippet": "",
        "labels": [],
    }
    res = await ActivityEnvironment().run(g.classify_email, msg, "")
    assert res["category"] == "important_read"
    # Third arg is the LLM's tags, cached so the fan-out survives a cache hit.
    g._triage_upsert.assert_awaited_once_with(
        "security-noreply@linkedin.com", "important_read", ["security"]
    )


@pytest.mark.asyncio
async def test_capped_cache_hit_reteaches_the_cache_so_a_stuck_sender_can_decay():
    """(#262) The cache branch used to return without touching `triage_state`,
    and only the LLM branch re-taught it — so a sender above the threshold with
    a wrong verdict was stuck permanently, correctable by nothing the classifier
    itself could do. The CAPPED verdict must be what goes back.
    """
    llm = _CountingLlm()
    g = _make(llm=llm, lookup={"category": "important_action", "n": 6, "confidence": 0.9, "tags": []})
    msg = {
        "id": "m",
        "sender": "Google <no-reply@accounts.google.com>",
        "subject": "Security Alert: Your one-time sign in code is 429718.",
        "snippet": "",
        "labels": [],
    }
    await ActivityEnvironment().run(g.classify_email, msg, "")
    g._triage_upsert.assert_awaited_once_with("no-reply@accounts.google.com", "important_read")
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_uncapped_cache_hit_leaves_the_cache_alone():
    """The re-teach is scoped to disagreement. Reinforcing on every hit would
    ratchet each sender's n and confidence up merely for sending mail, pinning
    every cached verdict at 1.0 and making the LLM unreachable for good.
    """
    llm = _CountingLlm()
    g = _make(llm=llm, lookup={"category": "important_action", "n": 6, "confidence": 0.9, "tags": []})
    msg = {
        "id": "m",
        "sender": "boss@co.com",
        "subject": "Can you sign this by Friday?",
        "snippet": "",
        "labels": [],
    }
    res = await ActivityEnvironment().run(g.classify_email, msg, "")
    assert res["category"] == "important_action"
    g._triage_upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_capped_sender_stops_short_circuiting_the_llm(db_pool):
    """(#262) end to end, against the real disagreement arithmetic: one capped
    cache hit drops a 0.9-confidence sender to 0.6 — below `_CACHE_MIN_CONF` —
    so the very next message from it reaches the classifier again instead of
    being answered from a verdict nothing could change.
    """
    sender = "zz262-stuck@example.com"
    await db_pool.execute("DELETE FROM triage_state WHERE email_addr = $1", sender)
    await db_pool.execute(
        "INSERT INTO triage_state (email_addr, state, metadata, updated_at) "
        "VALUES ($1, 'important_action', $2, now())",
        sender,
        # `tags` present (even empty) is what makes this a cacheable row —
        # a row without the key is treated as pre-tags legacy state and
        # deliberately falls through to the LLM. See test_triage_cache_tags.
        {"n": 6, "confidence": 0.9, "category": "important_action", "tags": []},
    )
    # No llm_client on purpose: a cache MISS lands on source='fallback', so
    # 'cache' below cannot be an accident of the default return shape.
    g = GmailActivities(gmail_credentials_file="x", gmail_token_dir="x", db_pool=db_pool)
    try:
        first = await ActivityEnvironment().run(
            g.classify_email,
            {"id": "m1", "sender": sender, "subject": "Security alert: new sign-in", "labels": []},
            "",
        )
        assert (first["source"], first["category"]) == ("cache", "important_read")
        row = await db_pool.fetchrow(
            "SELECT state, metadata FROM triage_state WHERE email_addr = $1", sender
        )
        meta = row["metadata"]
        meta = json.loads(meta) if isinstance(meta, str) else dict(meta)
        assert float(meta["confidence"]) == 0.6  # 0.9 - 0.3, one disagreement

        second = await ActivityEnvironment().run(
            g.classify_email,
            {"id": "m2", "sender": sender, "subject": "Invoice attached", "labels": []},
            "",
        )
        assert second["source"] != "cache"
    finally:
        await db_pool.execute("DELETE FROM triage_state WHERE email_addr = $1", sender)


@pytest.mark.asyncio
async def test_cap_leaves_real_action_mail_alone():
    """The cap keys on notification phrasing only. A no-reply biller asking for
    money is still important_action — this fails if the cap ever grows into a
    blanket "no-reply senders are never urgent" rule, which would silently eat
    overdue invoices.
    """
    llm = _CountingLlm(
        json.dumps({"category": "important_action", "confidence": 0.9, "tags": ["payments"]})
    )
    g = _make(llm=llm, lookup=None)
    msg = {
        "id": "m",
        "sender": "billing@aws.amazon.com",
        "subject": "Action required - Your AWS account is past due",
        "snippet": "",
        "labels": [],
    }
    res = await ActivityEnvironment().run(g.classify_email, msg, "")
    assert res["category"] == "important_action"


def test_cap_is_scoped_to_important_action():
    """Only the interrupting tier is capped — the cap must never quietly
    promote or demote the other three."""
    subject = "Security alert: new sign-in"
    for category in ("important_read", "informational", "useless"):
        assert cap_notification_category(category, subject, []) == category


def test_user_markers_extend_the_shared_list():
    """`extra_notification_markers` is how the user silences their own bank's
    phrasing without it being hardcoded in an open-source repo."""
    subject = "Axis Bank - Incorrect Login Attempt"
    assert cap_notification_category("important_action", subject, []) == "important_action"
    assert (
        cap_notification_category("important_action", subject, ["incorrect login attempt"])
        == "important_read"
    )


# --------------------------------------------------------------------------
# User sender rules
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sender_override_classifies_without_the_llm_or_the_cache():
    """A domain rule decides outright: no LLM call, and no `triage_state` write
    (deleting the rule must stop it applying, not leave learned state behind).
    """
    llm = _CountingLlm(json.dumps({"category": "important_action", "confidence": 0.9}))
    g = _make(
        llm=llm,
        lookup={"category": "important_action", "n": 9, "confidence": 1.0, "tags": []},
        rules={"sender_overrides": {"@substack.com": "informational"}},
    )
    msg = {
        "id": "m",
        "sender": "The Grey Swan <thegreyswan@substack.com>",
        "subject": "This week",
        "snippet": "",
        "labels": [],
    }
    res = await ActivityEnvironment().run(g.classify_email, msg, "")
    assert res["category"] == "informational"
    assert res["source"] == "override"
    assert res["tags"] == []
    assert llm.calls == 0
    g._triage_upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_sender_override_returns_its_own_tags_so_the_money_fanout_survives():
    """(#263) An override skips the LLM, so nothing else can produce tags — and
    `GmailIngestFlow` spawns MoneyProcessFlow on `financial`/`payments`. Before
    this, silencing a biller silently turned its receipt extraction off.
    Still no `triage_state` write: tags do not make a rule learned state.
    """
    llm = _CountingLlm(json.dumps({"category": "important_action", "confidence": 0.9}))
    g = _make(
        llm=llm,
        lookup=None,
        rules={
            "sender_overrides": {
                "billing@bank.example": {
                    "category": "important_read",
                    "tags": ["financial", "receipt"],
                }
            }
        },
    )
    msg = {
        "id": "m",
        "sender": "Bank <billing@bank.example>",
        "subject": "Your statement",
        "snippet": "",
        "labels": [],
    }
    res = await ActivityEnvironment().run(g.classify_email, msg, "")
    assert res["category"] == "important_read"
    assert res["source"] == "override"
    assert set(res["tags"]) & {"financial", "payments"}
    assert llm.calls == 0
    g._triage_upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_classification_survives_an_unreadable_rules_row():
    """A broken settings read must degrade to the defaults, never take email
    classification down with it."""

    class _AngryPool:
        async def fetchrow(self, *a, **k):
            raise RuntimeError("settings unavailable")

    llm = _CountingLlm(json.dumps({"category": "important_read", "confidence": 0.8}))
    g = _make(llm=llm, lookup=None)
    g.db_pool = _AngryPool()
    g._triage_lookup = AsyncMock(return_value=None)
    g._triage_upsert = AsyncMock(return_value=None)
    msg = {"id": "m", "sender": "a@b.com", "subject": "hi", "snippet": "", "labels": []}
    res = await ActivityEnvironment().run(g.classify_email, msg, "")
    assert res["category"] == "important_read"


# --------------------------------------------------------------------------
# Defect 1 — important_read must not hold mail unread
# --------------------------------------------------------------------------


class _RecordingService:
    def __init__(self, labels: list[str] | None = None):
        self.modify_calls: list[dict] = []
        self._labels = labels or []

    def users(self):
        return self

    def messages(self):
        return self

    def modify(self, **kwargs):
        self.modify_calls.append(kwargs)
        return _Req({"id": kwargs.get("id", "")})

    def get(self, id, **kwargs):
        return _Req({"id": id, "labelIds": self._labels})


class _Req:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


@pytest.mark.asyncio
async def test_important_read_verdict_clears_unread_and_keeps_important(monkeypatch):
    """Asserts the literal Gmail wire payload. Fails if the tier ever goes back
    to holding mail unread — the regression that grew the inbox to ~200 unread.
    """
    svc = _RecordingService()
    monkeypatch.setattr(
        "aegis_worker.activities.gmail._build_gmail_service", lambda *a, **k: svc
    )
    g = GmailActivities(gmail_credentials_file="/tmp/x.json", gmail_token_dir="/tmp")
    result = await ActivityEnvironment().run(g.apply_label, "sebas", "msg-1", "IMPORTANT_READ")
    assert result["ok"] is True
    assert svc.modify_calls == [
        {
            "userId": "me",
            "id": "msg-1",
            "body": {"addLabelIds": ["IMPORTANT"], "removeLabelIds": ["UNREAD"]},
        }
    ]


# --------------------------------------------------------------------------
# "Is it already read?" — the guard on interrupting
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_message_unread_reads_live_label_state(monkeypatch):
    g = GmailActivities(gmail_credentials_file="/tmp/x.json", gmail_token_dir="/tmp")
    env = ActivityEnvironment()

    monkeypatch.setattr(
        "aegis_worker.activities.gmail._build_gmail_service",
        lambda *a, **k: _RecordingService(labels=["INBOX", "UNREAD"]),
    )
    assert await env.run(g.is_message_unread, "sebas", "m") is True

    monkeypatch.setattr(
        "aegis_worker.activities.gmail._build_gmail_service",
        lambda *a, **k: _RecordingService(labels=["INBOX"]),
    )
    assert await env.run(g.is_message_unread, "sebas", "m") is False


@pytest.mark.asyncio
async def test_is_message_unread_fails_open(monkeypatch):
    """An API error must not silently swallow a real action item."""

    def _boom(*a, **k):
        raise RuntimeError("gmail down")

    monkeypatch.setattr("aegis_worker.activities.gmail._build_gmail_service", _boom)
    g = GmailActivities(gmail_credentials_file="/tmp/x.json", gmail_token_dir="/tmp")
    assert await ActivityEnvironment().run(g.is_message_unread, "sebas", "m") is True


# --------------------------------------------------------------------------
# Defect 3 — the Todoist disposition signal
# --------------------------------------------------------------------------


def test_sender_from_description_matches_capture_format():
    """Pins the coupling to `flows/gmail_ingest.py::_route`, which writes
    `From: <sender>` as the first line of every #email capture. If that format
    drifts this fails here rather than silently dropping every sender relearn.
    """
    description = (
        "From: Google <no-reply@accounts.google.com>\n\n"
        "Someone signed in to your account.\n\n"
        "[Open in Gmail](https://mail.google.com/mail/u/0/#inbox/abc)"
    )
    assert _sender_from_description(description) == "no-reply@accounts.google.com"
    assert _sender_from_description("Forwarded from: work\n\nblah") == ""
    assert _sender_from_description(None) == ""
