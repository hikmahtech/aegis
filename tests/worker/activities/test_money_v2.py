"""MoneyActivities v2 (spec §2, §5.4, §7.1) against a temp books repo."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis.services import books
from aegis.services import journal_index as ji
from aegis_worker.activities.money import MoneyActivities
from temporalio.testing import ActivityEnvironment

HAS_HLEDGER = shutil.which("hledger") is not None and shutil.which("git") is not None
pytestmark = pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")

ACCOUNTS = """commodity ₹ 1,00,000.00
commodity $ 1000.00
account assets:bank:hdfc:1225
account assets:unknown
account liabilities:card:axis:1313
account expenses:unknown
account expenses:saas
account expenses:media
account expenses:hikmah:unknown
account expenses:hikmah:saas
account income:unknown
account income:hikmah:other
account equity:transfers
"""
RULES = (
    "- match: 'stockopedia\\.com'\n  ignore: true\n"
    "- match: 'eleven labs'\n  entity: hikmah\n  account: expenses:hikmah:saas\n"
    "  payee: Eleven Labs\n"
)


def _repo(tmp_path: Path) -> books.BooksConfig:
    root = tmp_path / "books"
    (root / "personal").mkdir(parents=True)
    (root / "hikmah").mkdir()
    (root / "rules").mkdir()
    (root / "accounts.journal").write_text(ACCOUNTS)
    (root / "prices.journal").write_text("")
    (root / "recurring.journal").write_text("")
    (root / "personal" / "2026.journal").write_text("; p\n")
    (root / "hikmah" / "2026.journal").write_text("; h\n")
    (root / "rules" / "accounts.yaml").write_text(RULES)
    (root / "main.journal").write_text(
        "include accounts.journal\ninclude prices.journal\ninclude personal/2026.journal\n"
        "include hikmah/2026.journal\ninclude recurring.journal\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=root, check=True
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
        cwd=root, check=True,
    )
    return books.BooksConfig(path=root)


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def _clean(db_pool):
    await db_pool.execute("DELETE FROM finance.journal_index WHERE mailbox LIKE 'v2-%'")
    await db_pool.execute("DELETE FROM finance.receipt_email WHERE message_id LIKE 'v2-%'")
    yield
    await db_pool.execute("DELETE FROM finance.journal_index WHERE mailbox LIKE 'v2-%'")
    await db_pool.execute("DELETE FROM finance.receipt_email WHERE message_id LIKE 'v2-%'")


def _act(db_pool, cfg, llm=None, capture=None) -> MoneyActivities:
    return MoneyActivities(
        db_pool=db_pool, llm=llm, delivery=None, books_cfg=cfg,
        ignored_mailboxes=frozenset({"v2-stpd"}), mailbox_entities={"v2-hikmah": "hikmah"},
        capture=capture,
    )


def _receipt(mailbox="v2-personal", sender="HDFC Bank InstaAlerts <alerts@hdfcbank.bank.in>",
             subject="UPI txn", body="", message_id="v2-m1",
             rid="00000000-0000-0000-0000-000000000001"):
    return {"id": rid, "account": mailbox, "message_id": message_id, "sender": sender,
            "subject": subject, "body_plain": body, "received_at": "2026-09-02T10:00:00+00:00"}


HDFC_BODY = (
    "Rs.10.00 is debited from your account ending 1225 towards VPA q2@ybl (Jai shree nakoda) "
    "on 02-09-26. UPI transaction reference no.: 128932002048."
)


@pytest.mark.asyncio
async def test_parse_uses_bank_parser_then_rules_then_defaults(db_pool, tmp_path):
    act = _act(db_pool, _repo(tmp_path))
    ev = await ActivityEnvironment().run(act.parse_money_email, _receipt(body=HDFC_BODY))
    assert ev["parser"] == "hdfc_upi" and ev["entity"] == "personal"
    assert ev["account"] == "expenses:unknown" and ev["payee_key"] == "jai shree nakoda"
    assert ev["occurred_on"] == "2026-09-02"


@pytest.mark.asyncio
async def test_parse_ignored_mailbox_and_ignore_rule(db_pool, tmp_path):
    act = _act(db_pool, _repo(tmp_path))
    ev = await ActivityEnvironment().run(
        act.parse_money_email, _receipt(mailbox="v2-stpd", body=HDFC_BODY)
    )
    assert ev["kind"] == "ignore" and ev["entity"] == "none" and ev["parser"] == "mailbox"
    stripe = _receipt(
        sender='"LSEG Billing via Data" <data@stockopedia.com>', subject="New Invoice",
        body="Receipt from LSEG £22269.97 Paid August 2, 2026 Payment method - 1313",
    )
    llm = AsyncMock()
    llm.extract_money_batch = AsyncMock(return_value=[{
        "kind": "transaction", "direction": "out", "amount": "22269.97", "currency": "GBP",
        "payee": "LSEG Billing", "payee_key": "lseg billing", "channel": "receipt",
        "confidence": 0.9, "parser": "llm", "source_class": "receipt", "entity": "personal",
    }])
    act = _act(db_pool, _repo(tmp_path / "b"), llm=llm)
    ev = await ActivityEnvironment().run(act.parse_money_email, stripe)
    assert ev["kind"] == "ignore" and ev["entity"] == "none" and ev["parser"] == "llm+rule"


@pytest.mark.asyncio
async def test_parse_llm_low_confidence_lands_in_unknown_and_rule_sets_entity(db_pool, tmp_path):
    llm = AsyncMock()
    llm.extract_money_batch = AsyncMock(return_value=[{
        "kind": "transaction", "direction": "out", "amount": "1936.00", "currency": "INR",
        "payee": "Eleven Labs Inc.", "payee_key": "eleven labs inc", "channel": "receipt",
        "category": "saas", "confidence": 0.5, "parser": "llm", "source_class": "receipt",
        "entity": "personal", "occurred_on": "2026-08-25",
    }])
    act = _act(db_pool, _repo(tmp_path), llm=llm)
    ev = await ActivityEnvironment().run(
        act.parse_money_email,
        _receipt(sender="invoice+statements@stripe.com", subject="Receipt",
                 body="Receipt from Eleven Labs Rs 1936.00 paid 25 August 2026"),
    )
    # the rule wins over the low-confidence unknown: entity hikmah, account from the rule
    assert ev["entity"] == "hikmah" and ev["account"] == "expenses:hikmah:saas"
    assert ev["payee"] == "Eleven Labs"
    llm.extract_money_batch = AsyncMock(return_value=[{
        "kind": "transaction", "direction": "out", "amount": "50.00", "currency": "INR",
        "payee": "Some Shop", "payee_key": "some shop", "channel": "other",
        "category": "shopping", "confidence": 0.5, "parser": "llm", "source_class": "other",
        "entity": "personal",
    }])
    ev = await ActivityEnvironment().run(
        act.parse_money_email,
        _receipt(sender="x@y.com", subject="s", body="Rs 50.00 paid at Some Shop"),
    )
    assert ev["account"] == "expenses:unknown" and ev["occurred_on"] == "2026-09-02"


@pytest.mark.asyncio
async def test_parse_flags_llm_failure(db_pool, tmp_path):
    llm = AsyncMock()
    llm.extract_money_batch = AsyncMock(
        return_value=[{"kind": "ignore", "parser": "llm", "_parse_failed": True}]
    )
    act = _act(db_pool, _repo(tmp_path), llm=llm)
    ev = await ActivityEnvironment().run(
        act.parse_money_email,
        _receipt(sender="a@b.c", subject="s", body="Rs 100.00 charged to your card"),
    )
    assert ev.get("_parse_failed") is True


def _bank_event(**kw):
    base = {"kind": "transaction", "direction": "out", "amount": "10.00", "currency": "INR",
            "payee": "Jai shree nakoda", "payee_key": "jai shree nakoda", "channel": "upi",
            "instrument": "hdfc-1225", "occurred_on": "2026-09-02", "entity": "personal",
            "account": "expenses:unknown", "parser": "hdfc_upi", "confidence": 1.0,
            "source_class": "bank", "ref": "128932002048"}
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_post_then_receipt_links_and_enriches(db_pool, tmp_path):
    cfg = _repo(tmp_path)
    act = _act(db_pool, cfg)
    # A bank alert whose payee is a bare VPA — the "raw" shape a receipt may improve on.
    bank = _bank_event(payee="q203028199@ybl", payee_key="q203028199 ybl")
    r = await ActivityEnvironment().run(act.post_money_event, "rid1", "v2-personal", "m-bank", bank)
    assert r["status"] == "posted" and r["journal_file"] == "personal/2026.journal"
    receipt = _bank_event(payee="Apple Music Individual", payee_key="apple music individual",
                          channel="receipt", instrument=None, account="expenses:media",
                          parser="apple_receipt", source_class="receipt",
                          occurred_on="2026-09-03", ref=None)
    r2 = await ActivityEnvironment().run(
        act.post_money_event, "rid2", "v2-personal", "m-rcpt", receipt
    )
    assert r2["status"] == "linked" and r2["linked"] == "v2-personal/m-bank"
    assert r2["journal_file"] is None
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert "2026-09-02 * Apple Music Individual" in text
    assert "    expenses:media                          ₹10.00\n    assets:bank:hdfc:1225\n" in text
    assert "receipt: v2-personal/m-rcpt" in text
    assert text.count("; msgid:") == 1
    bank = await ji.get(db_pool, "v2-personal/m-bank")
    assert bank["linked_message_id"] == "v2-personal/m-rcpt" and bank["account"] == "expenses:media"
    assert bank["payee"] == "Apple Music Individual"


@pytest.mark.asyncio
async def test_receipt_then_bank_links_and_fixes_instrument(db_pool, tmp_path):
    cfg = _repo(tmp_path)
    act = _act(db_pool, cfg)
    receipt = _bank_event(payee="Eleven Labs", payee_key="eleven labs", channel="receipt",
                          instrument="card-1313", account="expenses:saas",
                          parser="stripe_receipt", source_class="receipt", ref=None)
    r = await ActivityEnvironment().run(
        act.post_money_event, "rid1", "v2-personal", "m-rcpt", receipt
    )
    assert r["status"] == "posted"
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert "    liabilities:card:axis:1313\n" in text
    bank = _bank_event(payee="ELEVENLABS", payee_key="elevenlabs", channel="card",
                       instrument="axis-cc-1313", parser="axis_card_spend",
                       occurred_on="2026-09-03")
    r2 = await ActivityEnvironment().run(
        act.post_money_event, "rid2", "v2-personal", "m-bank", bank
    )
    assert r2["status"] == "linked" and r2["linked"] == "v2-personal/m-rcpt"
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert text.count("; msgid:") == 1 and "bank: v2-personal/m-bank" in text
    assert "2026-09-02 * Eleven Labs" in text
    # A retry of the second-arriving email must not write a second block.
    # find_match excludes already-linked rows, so the linked branch would fall
    # through to post_event, whose msgid guard looks for a `; msgid:` line a
    # linked email never has — duplicating the payment in the ledger.
    r3 = await ActivityEnvironment().run(
        act.post_money_event, "rid2", "v2-personal", "m-bank", bank
    )
    assert (cfg.path / "personal" / "2026.journal").read_text() == text
    assert (r3["status"], r3["linked"]) == (r2["status"], r2["linked"])


@pytest.mark.asyncio
async def test_post_is_idempotent_and_indexes_dues_and_info(db_pool, tmp_path):
    cfg = _repo(tmp_path)
    act = _act(db_pool, cfg)
    await ActivityEnvironment().run(
        act.post_money_event, "rid1", "v2-personal", "m-bank", _bank_event()
    )
    r = await ActivityEnvironment().run(
        act.post_money_event, "rid1", "v2-personal", "m-bank", _bank_event()
    )
    assert r["status"] == "posted"
    assert (cfg.path / "personal" / "2026.journal").read_text().count("; msgid:") == 1
    due = _bank_event(kind="due", due_on="2026-09-07", channel="statement",
                      payee="Axis credit card XX13", payee_key="axis credit card xx13",
                      amount="100308.53")
    r = await ActivityEnvironment().run(
        act.post_money_event, "rid3", "v2-personal", "m-due", due, "task-9"
    )
    assert r["status"] == "indexed"
    assert (await ji.get(db_pool, "v2-personal/m-due"))["todoist_ref"] == "task-9"
    info = _bank_event(kind="info", amount=None, currency=None, direction=None)
    r = await ActivityEnvironment().run(
        act.post_money_event, "rid4", "v2-personal", "m-info", info
    )
    assert r["status"] == "indexed"


@pytest.mark.asyncio
async def test_payment_closes_its_open_due(db_pool, tmp_path):
    cfg = _repo(tmp_path)
    capture = AsyncMock()
    capture.complete_captured_task = AsyncMock(return_value=True)
    act = _act(db_pool, cfg, capture=capture)
    due = _bank_event(kind="due", due_on="2026-09-07", channel="statement",
                      payee="Axis credit card XX13", payee_key="axis credit card xx13",
                      amount="100308.53")
    await ActivityEnvironment().run(
        act.post_money_event, "rid3", "v2-personal", "m-due", due, "task-9"
    )
    paid = _bank_event(payee="Axis credit card XX13", payee_key="axis credit card xx13",
                       amount="100308.53", channel="imps", occurred_on="2026-09-06",
                       account="equity:transfers")
    r = await ActivityEnvironment().run(
        act.post_money_event, "rid5", "v2-personal", "m-paid", paid
    )
    assert r["closed_due"] == "v2-personal/m-due"
    capture.complete_captured_task.assert_awaited_once_with("task-9")
    assert (await ji.get(db_pool, "v2-personal/m-due"))["linked_message_id"] == "v2-personal/m-paid"


@pytest.mark.asyncio
async def test_a_due_the_noise_guards_left_untasked_still_closes(db_pool, tmp_path):
    """Every due `capture_due` refuses to task must still close when it is paid.

    `capture_due` has three noise guards — a zero invoice, a twin obligation
    already tasked under another payee's name, and an autopay notice — and all
    three index the due and withhold only the Todoist task. Nothing else writes
    `linked_message_id` for a due, and every "open dues" count in the product
    (`build_money_brief`, `build_month_close`, `/api/money/state`) reads exactly
    that column, so requiring a task ref to close one made all three
    structurally unclosable and the counter monotonic. Four of seven live bill
    mails on 2026-09-05 were autopay notices: the first month close would have
    said "still open: 4" for a month in which every one of them was paid, and
    that four would never have come down.

    The guards are driven for real here, not assumed: each case asserts
    `capture_due` returned no ref FIRST, so a guard that stopped firing would
    fail this test rather than quietly make it vacuous.
    """
    from aegis_worker.activities.capture import CaptureActivities

    cfg = _repo(tmp_path)
    capture = AsyncMock()
    capture.complete_captured_task = AsyncMock(return_value=True)
    act = _act(db_pool, cfg, capture=capture)
    guards = CaptureActivities(db_pool=db_pool, connector=AsyncMock(), todoist_projects={})

    def _due(**kw):
        return _bank_event(
            kind="due", channel="statement", instrument=None, occurred_on=None,
            due_on="2026-09-07", account=None, **kw,
        )

    # The twin guard needs an obligation already tasked under ANOTHER name at
    # the same amount, currency and due date — Google Pay mirroring a biller.
    mirror = _due(payee="Google Pay Axis cc", payee_key="google pay axis cc", amount="4242.00")
    await ActivityEnvironment().run(
        act.post_money_event, "rid-tw", "v2-personal", "m-mirror", mirror, "task-mirror"
    )

    cases = {
        # A ₹0 invoice, which Cloudflare/Workspace/AWS send routinely.
        "zero": _due(payee="Cloudflare", payee_key="cloudflare", amount="0.00"),
        # The biller's own statement for the bill Google Pay already mirrored.
        "twin": _due(payee="Axis credit card XX13", payee_key="axis credit card xx13",
                     amount="4242.00"),
        # "Pay Apple Fitness+ ₹149.00" is a task nobody can act on.
        "autopay": _due(payee="Apple Fitness Plus", payee_key="apple fitness plus",
                        amount="149.00", autopay=True),
    }
    for name, due in cases.items():
        ref = await ActivityEnvironment().run(guards.capture_due, due, "v2-personal", f"g-{name}")
        assert ref is None, f"the {name} guard did not fire — this test would be vacuous"
        r = await ActivityEnvironment().run(
            act.post_money_event, f"rid-{name}", "v2-personal", f"m-{name}", due, ref
        )
        assert r["status"] == "indexed"
        row = await ji.get(db_pool, f"v2-personal/m-{name}")
        assert row["todoist_ref"] is None and row["linked_message_id"] is None

    for name, due in cases.items():
        paid = _bank_event(
            payee=due["payee"], payee_key=due["payee_key"], amount=due["amount"],
            channel="imps", occurred_on="2026-09-06", account="equity:transfers",
        )
        r = await ActivityEnvironment().run(
            act.post_money_event, f"rid-{name}-p", "v2-personal", f"m-{name}-paid", paid
        )
        assert r["closed_due"] == f"v2-personal/m-{name}", (name, r)
        row = await ji.get(db_pool, f"v2-personal/m-{name}")
        assert row["linked_message_id"] == f"v2-personal/m-{name}-paid"

    # Nothing was sent to Todoist for a due that never had a task. A NULL ref
    # reaching `complete_captured_task` would be a Todoist call on `None`.
    capture.complete_captured_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_after_a_failed_due_link_does_not_close_twice(db_pool, tmp_path, monkeypatch):
    """The reachable double-close: the Todoist task is completed, then the
    activity dies before `ji.mark_due_paid` records it, and Temporal retries.

    A plain "run it twice" would prove nothing — the first run's
    `mark_due_paid` already takes the due out of `find_open_due`, so the
    second run skips the close whether or not the short-circuit exists. The
    crash has to land in the window between the close and that write.
    """
    cfg = _repo(tmp_path)
    capture = AsyncMock()
    capture.complete_captured_task = AsyncMock(return_value=True)
    act = _act(db_pool, cfg, capture=capture)
    due = _bank_event(kind="due", due_on="2026-09-07", channel="statement",
                      payee="Axis credit card XX13", payee_key="axis credit card xx13",
                      amount="100308.53")
    await ActivityEnvironment().run(
        act.post_money_event, "rid3", "v2-personal", "m-due", due, "task-9"
    )
    paid = _bank_event(payee="Axis credit card XX13", payee_key="axis credit card xx13",
                       amount="100308.53", channel="imps", occurred_on="2026-09-06",
                       account="equity:transfers")

    real_mark = ji.mark_due_paid
    calls = {"n": 0}

    async def flaky_mark(pool, due_msgid, payment_msgid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("worker died after completing the Todoist task")
        await real_mark(pool, due_msgid, payment_msgid)

    monkeypatch.setattr("aegis_worker.activities.money.ji.mark_due_paid", flaky_mark)
    with pytest.raises(RuntimeError):
        await ActivityEnvironment().run(
            act.post_money_event, "rid5", "v2-personal", "m-paid", paid
        )
    r = await ActivityEnvironment().run(
        act.post_money_event, "rid5", "v2-personal", "m-paid", paid
    )
    assert r["status"] == "posted" and r["journal_file"] == "personal/2026.journal"
    capture.complete_captured_task.assert_awaited_once_with("task-9")
    assert (cfg.path / "personal" / "2026.journal").read_text().count("; msgid:") == 1


@pytest.mark.asyncio
async def test_closing_a_due_keeps_the_payments_own_counterpart_link(db_pool, tmp_path):
    """The normal credit-card case: the bank alert posts first but its raw
    payee does not match the due, so the receipt is the email that BOTH links
    to the bank alert and closes the due. `ji.link` writes both sides, so
    using it for the close would overwrite the receipt's link to the bank
    alert while the bank alert still pointed back at the receipt.
    """
    cfg = _repo(tmp_path)
    capture = AsyncMock()
    capture.complete_captured_task = AsyncMock(return_value=True)
    act = _act(db_pool, cfg, capture=capture)
    due = _bank_event(kind="due", due_on="2026-09-07", channel="statement",
                      payee="Axis credit card XX13", payee_key="axis credit card xx13",
                      amount="100308.53")
    await ActivityEnvironment().run(
        act.post_money_event, "rid1", "v2-personal", "m-due", due, "task-9"
    )
    # The bank alert's payee is the card network's descriptor, so find_open_due
    # misses and the due stays open.
    bank = _bank_event(payee="AXISCC PMT", payee_key="axiscc pmt", amount="100308.53",
                       channel="imps", occurred_on="2026-09-06", account="equity:transfers")
    r1 = await ActivityEnvironment().run(
        act.post_money_event, "rid2", "v2-personal", "m-bank", bank
    )
    assert r1["status"] == "posted" and r1["closed_due"] is None
    receipt = _bank_event(payee="Axis credit card XX13", payee_key="axis credit card xx13",
                          amount="100308.53", channel="receipt", instrument=None,
                          account="equity:transfers", parser="axis_cc_receipt",
                          source_class="receipt", occurred_on="2026-09-06", ref=None)
    r2 = await ActivityEnvironment().run(
        act.post_money_event, "rid3", "v2-personal", "m-rcpt", receipt
    )
    assert r2["status"] == "linked"
    assert r2["linked"] == "v2-personal/m-bank"
    assert r2["closed_due"] == "v2-personal/m-due"
    rows = {k: await ji.get(db_pool, f"v2-personal/{k}") for k in ("m-bank", "m-rcpt", "m-due")}
    # the payment keeps its own counterpart link, and the returned value agrees
    assert rows["m-rcpt"]["linked_message_id"] == r2["linked"] == "v2-personal/m-bank"
    assert rows["m-bank"]["linked_message_id"] == "v2-personal/m-rcpt"
    # and the due points at the payment, so it is out of the open-due pool
    assert rows["m-due"]["linked_message_id"] == "v2-personal/m-rcpt"


@pytest.mark.asyncio
async def test_books_disabled_still_indexes(db_pool, tmp_path):
    act = _act(db_pool, books.BooksConfig(path=tmp_path / "nowhere"))
    r = await ActivityEnvironment().run(
        act.post_money_event, "rid1", "v2-personal", "m-bank", _bank_event()
    )
    assert r["status"] == "books_disabled"
    assert (await ji.get(db_pool, "v2-personal/m-bank")) is not None


@pytest.mark.asyncio
async def test_store_money_result_marks_version_2(db_pool, tmp_path):
    act = _act(db_pool, _repo(tmp_path))
    async with db_pool.acquire() as conn:
        rid = await conn.fetchval(
            "INSERT INTO finance.receipt_email (message_id, account, sender, subject, "
            "received_at, parsed) "
            "VALUES ('v2-store', 'v2-personal', 's', 'j', now(), '{\"body_text\": \"b\"}') "
            "RETURNING id"
        )
    await ActivityEnvironment().run(
        act.store_money_result, str(rid), _bank_event(), "personal/2026.journal"
    )
    parsed = await db_pool.fetchval(
        "SELECT parsed FROM finance.receipt_email WHERE id = $1", rid
    )
    assert parsed["version"] == 2 and parsed["body_text"] == "b"
    assert parsed["journal_file"] == "personal/2026.journal"
    assert parsed["event"]["payee"] == "Jai shree nakoda"


@pytest.mark.asyncio
async def test_receipt_posts_its_own_block_when_the_matched_block_is_gone(db_pool, tmp_path, caplog):
    """The index can outlive the journal — a lost unpushed commit, a re-clone, a
    human revert. `find_match` only checks that the row HAS a journal_file, so
    the enrichment then rewrites a block that is not there and `rewrite_event`
    raises a plain BooksError. Uncaught, the activity retried forever and the
    row stuck; an extra block is recoverable, a stuck activity is not."""
    cfg = _repo(tmp_path)
    act = _act(db_pool, cfg)
    journal = cfg.path / "personal" / "2026.journal"
    await ActivityEnvironment().run(
        act.post_money_event, "rid1", "v2-personal", "m-bank", _bank_event()
    )
    journal.write_text("; p\n")  # the block the index still points at, gone

    receipt = _bank_event(payee="Apple Music Individual", payee_key="apple music individual",
                          channel="receipt", instrument=None, account="expenses:media",
                          parser="apple_receipt", source_class="receipt",
                          occurred_on="2026-09-03", ref=None)
    with caplog.at_level("WARNING", logger="temporalio.activity"):
        r = await ActivityEnvironment().run(
            act.post_money_event, "rid2", "v2-personal", "m-rcpt", receipt
        )

    assert r["status"] == "posted" and r["journal_file"] == "personal/2026.journal"
    assert r["linked"] is None
    # The fallback is the one path that can leave ONE payment in the journal
    # twice, so the warning has to say so — it is the only thread the weekly
    # brief's reader has to pull on.
    logged = " ".join(rec.getMessage() for rec in caplog.records)
    assert "money_enrich_failed" in logged and "duplicate" in logged.lower()
    assert "2026-09-03 * Apple Music Individual" in journal.read_text()
    # The index says what actually happened: posted, not linked.
    row = await ji.get(db_pool, "v2-personal/m-rcpt")
    assert row["journal_file"] == "personal/2026.journal"
    assert row["linked_message_id"] is None
    assert (await ji.get(db_pool, "v2-personal/m-bank"))["linked_message_id"] is None


@pytest.mark.asyncio
async def test_bank_posts_its_own_block_when_the_matched_block_is_gone(db_pool, tmp_path):
    """The same recovery on the other enrichment path."""
    cfg = _repo(tmp_path)
    act = _act(db_pool, cfg)
    journal = cfg.path / "personal" / "2026.journal"
    receipt = _bank_event(payee="Eleven Labs", payee_key="eleven labs", channel="receipt",
                          instrument="card-1313", account="expenses:saas",
                          parser="stripe_receipt", source_class="receipt", ref=None)
    await ActivityEnvironment().run(
        act.post_money_event, "rid1", "v2-personal", "m-rcpt", receipt
    )
    journal.write_text("; p\n")

    bank = _bank_event(payee="ELEVENLABS", payee_key="elevenlabs", channel="card",
                       instrument="axis-cc-1313", parser="axis_card_spend",
                       occurred_on="2026-09-03")
    r = await ActivityEnvironment().run(
        act.post_money_event, "rid2", "v2-personal", "m-bank", bank
    )

    assert r["status"] == "posted" and r["journal_file"] == "personal/2026.journal"
    assert r["linked"] is None
    text = journal.read_text()
    assert "2026-09-03 * ELEVENLABS" in text and "    liabilities:card:axis:1313\n" in text
    row = await ji.get(db_pool, "v2-personal/m-bank")
    assert row["journal_file"] == "personal/2026.journal" and row["linked_message_id"] is None


@pytest.mark.asyncio
async def test_a_dateless_receipt_parser_gets_its_date_from_received_at(db_pool, tmp_path):
    """`parse_airtel_receipt` returns a transaction with NO occurred_on — the
    body has no date. It is postable only because parse_money_email back-fills
    from `received_at` in the home timezone. Drop that step and `post_event`
    raises BooksError on every Airtel payment."""
    act = _act(db_pool, _repo(tmp_path))
    ev = await ActivityEnvironment().run(
        act.parse_money_email,
        _receipt(
            sender="Airtel <update@airtel.com>",
            subject="Here is your Airtel payment receipt!",
            body="Payment Reciept Dear SOMEONE . Thank you for choosing Airtel. We have "
                 "received a payment of Rs 5306.46 for your Bill Payment.",
        ),
    )
    assert ev["parser"] == "airtel_receipt" and ev["kind"] == "transaction"
    assert ev["occurred_on"] == "2026-09-02"  # received_at 10:00Z -> 15:30 IST


@pytest.mark.asyncio
async def test_a_block_the_chart_rejects_is_indexed_but_not_posted(db_pool, tmp_path):
    """`hledger check --strict` refusing a block is not a reason to fail
    forever. Only BooksDisabled was caught here, so a chart mismatch — an
    undeclared commodity or account — burned all three attempts, failed the
    workflow, and left the row below version 2 for the weekly sweep to
    re-drive and fail again, every week. Same stuck-row class as the
    enrichment path. The event is indexed WITHOUT a journal_file so the row
    exists, the sweep can retry once the chart is fixed, and `find_match`
    never offers a counterpart a block that was never written."""
    cfg = _repo(tmp_path)
    act = _act(db_pool, cfg)
    journal = cfg.path / "personal" / "2026.journal"
    before = journal.read_text()

    # XYZ is three valid ISO letters and an UNDECLARED commodity: the chart
    # declares only the rupee and the dollar.
    ev = _bank_event(currency="XYZ")
    r = await ActivityEnvironment().run(
        act.post_money_event, "rid1", "v2-personal", "m-chart", ev
    )

    assert r["status"] == "post_failed"
    assert r["journal_file"] is None and r["linked"] is None
    row = await ji.get(db_pool, "v2-personal/m-chart")
    assert row is not None and row["journal_file"] is None
    assert row["linked_message_id"] is None
    assert journal.read_text() == before  # the write reverted


@pytest.mark.asyncio
async def test_an_undeclared_account_also_lands_in_post_failed(db_pool, tmp_path):
    """The same handler by the other route, and the harder one to reach: an
    undeclared account normally CANNOT fail the check, because `post_event`
    maps one to the entity's unknown account first. It fails when the chart
    lacks that unknown account itself — a books repo whose chart was never
    finished — and then every personal expense is refused."""
    cfg = _repo(tmp_path)
    chart = cfg.path / "accounts.journal"
    chart.write_text(ACCOUNTS.replace("account expenses:unknown\n", ""))
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-aqm", "no unknown"],
        cwd=cfg.path, check=True,
    )
    journal = cfg.path / "personal" / "2026.journal"
    before = journal.read_text()

    r = await ActivityEnvironment().run(
        _act(db_pool, cfg).post_money_event, "rid1", "v2-personal", "m-nochart", _bank_event()
    )

    assert r["status"] == "post_failed" and r["journal_file"] is None
    row = await ji.get(db_pool, "v2-personal/m-nochart")
    assert row is not None and row["journal_file"] is None
    assert journal.read_text() == before  # the write reverted


@pytest.mark.asyncio
async def test_parse_marks_a_charge_that_happens_by_itself(db_pool, tmp_path):
    """The notices that became chores live on 2026-09-05 all say so in the
    body: Apple "automatically renews", AWS "scheduled to automatically
    renew", Axis "auto debit payment is due". None of them is a bill anyone
    has to pay, so the parse stamps them and `capture_due` drops the task.
    """
    llm = AsyncMock()
    llm.extract_money_batch = AsyncMock(return_value=[{
        "kind": "due", "direction": "out", "amount": "149.00", "currency": "INR",
        "payee": "Apple Fitness+", "payee_key": "apple fitness", "channel": "bill",
        "due_on": "2099-07-14", "confidence": 0.9, "parser": "llm",
        "source_class": "receipt",
    }])
    act = _act(db_pool, _repo(tmp_path), llm=llm)
    ev = await ActivityEnvironment().run(
        act.parse_money_email,
        _receipt(sender="no_reply@apple.com", subject="Your Subscription Renewal",
                 body="Starting from 14 July 2099, your subscription automatically "
                      "renews for Rs 149.00/month."),
    )
    assert ev["kind"] == "due" and ev["autopay"] is True

    # A real bill keeps its task: nothing in it says the money moves by itself.
    llm.extract_money_batch = AsyncMock(return_value=[{
        "kind": "due", "direction": "out", "amount": "8100.00", "currency": "INR",
        "payee": "Mahavitaran", "payee_key": "mahavitaran", "channel": "bill",
        "due_on": "2099-08-11", "confidence": 0.9, "parser": "llm",
        "source_class": "receipt",
    }])
    ev = await ActivityEnvironment().run(
        act.parse_money_email,
        _receipt(sender="billing@mahadiscom.in", subject="Your electricity bill",
                 body="Your bill of Rs 8100.00 is ready. Pay by 11-08-2099."),
    )
    assert ev["autopay"] is False


@pytest.mark.asyncio
async def test_parse_does_not_pay_the_llm_for_mail_with_no_money_in_it(db_pool, tmp_path):
    """Money-free mail never reaches the extractor.

    On 2026-09-05 the backfill spent 522,846 tokens on 324 extractions and
    tripped the governor's kill switch, which then blocked every LLM call in
    AEGIS — email triage included. Most of it went on mail triage correctly
    tags `financial` that holds no transaction: NSE and BSE alerts, GST portal
    notices, Groww digests, KDP royalty reports.
    """
    llm = AsyncMock()
    llm.extract_money_batch = AsyncMock(return_value=[])
    act = _act(db_pool, _repo(tmp_path), llm=llm)

    ev = await ActivityEnvironment().run(
        act.parse_money_email,
        _receipt(sender="nse_alerts@nse.co.in", subject="NSE Circular: trading holiday",
                 body="The exchange will be closed on 2 October 2099."),
    )
    assert ev["kind"] == "info" and ev["parser"] == "no_amount"
    llm.extract_money_batch.assert_not_awaited()

    # The stray '96' html_to_text emits (issue #381) is not an amount either —
    # it produced a fabricated ₹96 WazirX transaction in the live books.
    ev = await ActivityEnvironment().run(
        act.parse_money_email,
        _receipt(sender="noreply@wazirx.com", subject="Complete Your Re-KYC",
                 body="96\nDeposit Completed!\nYour Re-KYC is still pending."),
    )
    assert ev["kind"] == "info" and ev["parser"] == "no_amount"
    llm.extract_money_batch.assert_not_awaited()

    # A real receipt still reaches the extractor, including AWS's no-space form.
    llm.extract_money_batch = AsyncMock(return_value=[{
        "kind": "due", "direction": "out", "amount": "2068.12", "currency": "INR",
        "payee": "Amazon Web Services", "payee_key": "amazon web services",
        "channel": "bill", "confidence": 0.9, "parser": "llm", "source_class": "receipt",
    }])
    ev = await ActivityEnvironment().run(
        act.parse_money_email,
        _receipt(sender="no-reply@amazonaws.com", subject="AWS Billing Statement Available",
                 body="Your total amount is: INR2,068.12."),
    )
    assert ev["kind"] == "due" and ev["amount"] == "2068.12"
    llm.extract_money_batch.assert_awaited()


@pytest.mark.asyncio
async def test_a_transaction_with_no_amount_is_not_a_transaction(db_pool, tmp_path):
    """14 live index rows are notification mail the extractor called a
    `transaction` with no amount in it (issue #394): Anthropic "Your Max
    subscription is confirmed", Route 53 "Automatic renewal succeeded", Amazon
    Pay "Update on refund processed".

    Every amount-bearing transaction in the live index is posted and every
    amountless one is not, so these are not lost money and not an extraction
    failure to retry — the amount is simply absent from the mail. Left labelled
    `transaction` they inflate the transaction count and sit in the index as
    rows nothing can ever post, and they never stop costing: the writer refuses
    them, `post_failed` leaves `parsed.version` below 2, and the stuck-receipt
    sweep re-extracts the same mail every week.
    """
    llm = AsyncMock()
    llm.extract_money_batch = AsyncMock(return_value=[{
        "kind": "transaction", "direction": "out", "amount": None, "currency": None,
        "payee": "Anthropic", "payee_key": "anthropic", "channel": "card",
        "confidence": 0.6, "parser": "llm", "source_class": "other",
    }])
    act = _act(db_pool, _repo(tmp_path), llm=llm)
    confirmation = _receipt(
        sender="billing@anthropic.com", subject="Your Max subscription is confirmed",
        body="Your Max subscription is confirmed. Your plan renews at $200.00 a month.",
    )
    ev = await ActivityEnvironment().run(act.parse_money_email, confirmation)

    assert ev["kind"] == "info"
    # Why it is info, and the mail it came from, both survive the demotion.
    assert ev["parser"] == "llm+no_amount" and ev["payee"] == "Anthropic"
    # No account and no date were invented for it: both fallbacks are for
    # transactions, and inventing them is what made these rows look postable.
    assert ev["account"] is None and ev["occurred_on"] is None

    # The same extractor output WITH an amount is still a transaction, so the
    # gate is keyed on the missing amount and not on the kind.
    llm.extract_money_batch = AsyncMock(return_value=[{
        "kind": "transaction", "direction": "out", "amount": "200.00", "currency": "USD",
        "payee": "Anthropic", "payee_key": "anthropic", "channel": "card",
        "confidence": 0.6, "parser": "llm", "source_class": "other",
    }])
    ev = await ActivityEnvironment().run(act.parse_money_email, confirmation)
    assert ev["kind"] == "transaction" and ev["amount"] == "200.00"
    assert ev["occurred_on"] == "2026-09-02"

    # A due with no amount stays a due: an obligation whose size the mail did
    # not state is still an obligation worth surfacing, and `capture_due`
    # already refuses to raise a task for one.
    llm.extract_money_batch = AsyncMock(return_value=[{
        "kind": "due", "direction": "out", "amount": None, "currency": "INR",
        "payee": "Mahavitaran", "payee_key": "mahavitaran", "channel": "bill",
        "due_on": "2099-08-11", "confidence": 0.9, "parser": "llm", "source_class": "receipt",
    }])
    ev = await ActivityEnvironment().run(
        act.parse_money_email,
        _receipt(sender="billing@mahadiscom.in", subject="Your electricity bill",
                 body="Your bill is ready. Pay Rs. 0.00 shown online by 11-08-2099."),
    )
    assert ev["kind"] == "due" and ev["amount"] is None and ev["parser"] == "llm"

    # ...and so does a failed payment. That is the backstop that catches an
    # automatic debit which did not go through.
    llm.extract_money_batch = AsyncMock(return_value=[{
        "kind": "failed", "direction": "out", "amount": None, "currency": "INR",
        "payee": "Mahavitaran", "payee_key": "mahavitaran", "channel": "bill",
        "confidence": 0.9, "parser": "llm", "source_class": "receipt",
    }])
    ev = await ActivityEnvironment().run(
        act.parse_money_email,
        _receipt(sender="billing@mahadiscom.in", subject="Payment failed",
                 body="We could not collect Rs. 0.00 from your account."),
    )
    assert ev["kind"] == "failed" and ev["amount"] is None
