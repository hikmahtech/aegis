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
        db_pool=db_pool, llm=llm, delivery=None, fx_rates={}, books_cfg=cfg,
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
        _receipt(sender="invoice+statements@stripe.com", subject="Receipt", body="x"),
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
        act.parse_money_email, _receipt(sender="x@y.com", subject="s", body="b")
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
        act.parse_money_email, _receipt(sender="a@b.c", subject="s", body="b")
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
