"""StatementActivities against a real books repo and a real database.

This file exists because its absence let four defects into the lane at once.
`reconcile_statements` is where the pieces meet — the matcher, the poster, the
watermark and the hub sweep — and none of that wiring is exercised by any test
of the parts. A review found a `TypeError` on an argument `post_statement` does
not take, an outcome filter that blinded transfer pairing to the far side, and
an exception class that escaped the per-statement guard and killed the whole
tick. Every one of them lives in the seam, and every one of them would have
been caught by the first test that called the activity.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from aegis.services import books
from aegis.services.statements import row_id_for
from aegis_worker.activities import statements as statements_mod
from aegis_worker.activities.statements import StatementActivities
from temporalio.testing import ActivityEnvironment

HAS_HLEDGER = shutil.which("hledger") is not None and shutil.which("git") is not None
pytestmark = pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")

ACCOUNTS = """commodity ₹ 1,00,000.00
account assets:bank:hdfc:1225
account assets:bank:axis:9640
account liabilities:card:axis:1313
account expenses:unknown
account assets:unknown
account income:unknown
account expenses:fees
account equity:transfers
"""

FOLDERS = {
    "accounts": {
        "hdfc-1225": {"folder_id": "f1", "entities": ["personal"], "post_entity": "personal"},
        "axis-9640": {"folder_id": "f2", "entities": ["hikmah"], "post_entity": "hikmah"},
    }
}


def _repo(tmp_path: Path) -> books.BooksConfig:
    root = tmp_path / "books"
    (root / "personal").mkdir(parents=True)
    (root / "hikmah").mkdir()
    (root / "rules").mkdir()
    (root / "accounts.journal").write_text(ACCOUNTS)
    (root / "prices.journal").write_text("")
    (root / "personal" / "2026.journal").write_text("; p\n")
    (root / "hikmah" / "2026.journal").write_text("; h\n")
    (root / "rules" / "accounts.yaml").write_text("- match: 'ATM'\n  account: expenses:fees\n")
    (root / "main.journal").write_text(
        "include accounts.journal\ninclude prices.journal\n"
        "include personal/2026.journal\ninclude hikmah/2026.journal\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=root, check=True
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "i"],
        cwd=root, check=True,
    )
    return books.BooksConfig(path=root)


async def _row(pool, instrument, day, direction, amount, narration, statement_id, balance=None):
    occurred = date.fromisoformat(day)
    money = Decimal(amount)
    after = Decimal(balance) if balance is not None else None
    rid = row_id_for(
        instrument=instrument, occurred_on=occurred, direction=direction, amount=money,
        balance_after=after, occurrence_index=0, narration=narration,
    )
    await pool.execute(
        "INSERT INTO finance.statement_rows (row_id, instrument, occurred_on, narration, "
        "direction, amount, balance_after, statement_id, file_sha256) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'t') ON CONFLICT DO NOTHING",
        rid, instrument, occurred, narration, direction, money, after, statement_id,
    )
    return rid


async def _statement(pool, instrument, start, end, opening, closing, rows):
    sid = f"{instrument}/{start}..{end}"
    await pool.execute(
        "INSERT INTO finance.statements (statement_id, instrument, period_start, period_end, "
        "opening_balance, closing_balance, file_sha256, rows) VALUES ($1,$2,$3,$4,$5,$6,'t',$7) "
        "ON CONFLICT (statement_id) DO NOTHING",
        sid, instrument, date.fromisoformat(start), date.fromisoformat(end),
        Decimal(opening), Decimal(closing), rows,
    )
    return sid


_WIPE = (
    # The lane now indexes what it posts, so a row survives into the next test
    # and the matcher offers it as a candidate — which quietly changes what the
    # next statement does.
    "DELETE FROM finance.journal_index WHERE mailbox IN ('statement','st-box')",
    "DELETE FROM finance.statement_rows WHERE file_sha256 = 't'",
    "DELETE FROM finance.statements WHERE file_sha256 = 't'",
    "DELETE FROM finance.reconciled_through WHERE instrument IN ('hdfc-1225','axis-9640')",
    "DELETE FROM settings WHERE key = 'integration:statement_folders'",
    # The digest's once-a-month marker. A leftover from one test makes the next
    # one's run produce no digest at all, which is the shape of a passing test
    # that proves nothing.
    "DELETE FROM settings WHERE key = 'statement_digest_month'",
)


@pytest_asyncio.fixture(loop_scope="function")
async def clean(db_pool):
    for sql in _WIPE:
        await db_pool.execute(sql)
    await db_pool.execute(
        "INSERT INTO settings (key, value) VALUES ('integration:statement_folders', $1) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        FOLDERS,
    )
    yield db_pool
    for sql in _WIPE:
        await db_pool.execute(sql)


def _act(pool, cfg) -> StatementActivities:
    return StatementActivities(db_pool=pool, gmail_token_dir="config/", books_cfg=cfg)


async def test_reconcile_posts_a_statement_and_moves_the_watermark(clean, tmp_path):
    """The whole seam, once, with real money.

    Every argument this activity hands to `post_statement` is checked by
    running it: a name that function does not take is a `TypeError` the caller
    does not catch, under a NO_RETRY flow, so the tick dies before the sweep and
    the digest and does so again every day. That is what shipped, and no test of
    the parts could see it.
    """
    cfg = _repo(tmp_path)
    sid = await _statement(clean, "hdfc-1225", "2026-07-01", "2026-07-31", "0", "-500", 1)
    await _row(clean, "hdfc-1225", "2026-07-10", "out", "500.00", "ATM WITHDRAWAL", sid, "-500")

    out = await ActivityEnvironment().run(_act(clean, cfg).reconcile_statements, True, "")

    assert out["status"] == "ok", out
    assert out["posted"] == 1, out
    assert [r["status"] for r in out["results"]] == ["posted"]
    watermark = await clean.fetchval(
        "SELECT through_date FROM finance.reconciled_through WHERE instrument = 'hdfc-1225'"
    )
    assert watermark == date(2026, 7, 31)
    assert await clean.fetchval(
        "SELECT reconciled_at IS NOT NULL FROM finance.statements WHERE statement_id = $1", sid
    )
    # The row really reached the journal, and the rule really applied.
    assert "expenses:fees" in (cfg.path / "personal" / "2026.journal").read_text()


async def test_a_dry_run_reports_what_it_would_write_and_writes_nothing(clean, tmp_path):
    """`post=False` is the mode a person reads before letting a schedule near
    the books, so it has to produce a plan rather than skip silently. It used to
    `continue` before ever calling the poster, so the seed's promise of a
    readable dry run was empty and the first signal of any defect above would
    have been the first live run."""
    cfg = _repo(tmp_path)
    before = (cfg.path / "personal" / "2026.journal").read_text()
    sid = await _statement(clean, "hdfc-1225", "2026-07-01", "2026-07-31", "0", "-500", 1)
    await _row(clean, "hdfc-1225", "2026-07-10", "out", "500.00", "ATM WITHDRAWAL", sid, "-500")

    out = await ActivityEnvironment().run(_act(clean, cfg).reconcile_statements, False, "")

    assert [r["status"] for r in out["results"]] == ["would_post"]
    assert out["results"][0]["posted"] == 1
    assert (cfg.path / "personal" / "2026.journal").read_text() == before
    assert await clean.fetchval("SELECT count(*) FROM finance.reconciled_through") == 0


async def test_one_failing_statement_does_not_take_the_rest_of_the_run_with_it(
    clean, tmp_path, monkeypatch
):
    """The flow is NO_RETRY, so an exception escaping this loop costs the
    findings sweep, the digest and every statement after it — daily. Only
    `BooksCheckError` was caught, and a commodity hledger cannot price raises
    `decimal.InvalidOperation`, which is not one."""
    cfg = _repo(tmp_path)
    bad = await _statement(clean, "axis-9640", "2026-07-01", "2026-07-31", "0", "-100", 1)
    await _row(clean, "axis-9640", "2026-07-05", "out", "100.00", "SOMETHING", bad, "-100")
    good = await _statement(clean, "hdfc-1225", "2026-07-01", "2026-07-31", "0", "-500", 1)
    await _row(clean, "hdfc-1225", "2026-07-10", "out", "500.00", "ATM WITHDRAWAL", good, "-500")

    from aegis.services import statement_post as sp

    real = sp.post_statement

    async def boom(statement, *a, **kw):
        if statement.instrument == "axis-9640":
            raise ArithmeticError("cannot price $")
        return await real(statement, *a, **kw)

    monkeypatch.setattr(sp, "post_statement", boom)
    out = await ActivityEnvironment().run(_act(clean, cfg).reconcile_statements, True, "")

    by_id = {r["statement"]: r["status"] for r in out["results"]}
    assert by_id[bad] == "failed"
    assert by_id[good] == "posted"
    # The run still finished its job: findings were swept and a digest produced.
    assert "findings" in out and out["digest"]


async def test_a_statement_before_the_scope_date_is_reported_and_never_posted(clean, tmp_path):
    """The Drive folder holds an FY2024-25 statement of 1,619 rows that predates
    the books by two years. Posting it is a decision about what the ledger is
    for, not something a schedule does because the file is there."""
    cfg = _repo(tmp_path)
    old = await _statement(clean, "hdfc-1225", "2024-05-01", "2024-05-31", "0", "-500", 1)
    await _row(clean, "hdfc-1225", "2024-05-10", "out", "500.00", "OLD ATM", old, "-500")

    out = await ActivityEnvironment().run(
        _act(clean, cfg).reconcile_statements, True, "2026-07-01"
    )

    assert [r["status"] for r in out["results"]] == ["out_of_scope"]
    assert out["posted"] == 0
    assert "OLD ATM" not in (cfg.path / "personal" / "2026.journal").read_text()


async def test_an_unconfigured_lane_skips_rather_than_failing(clean, tmp_path):
    """A fork of AEGIS has no Drive folder. That is not a broken flow."""
    await clean.execute("DELETE FROM settings WHERE key = 'integration:statement_folders'")
    out = await ActivityEnvironment().run(
        _act(clean, _repo(tmp_path)).reconcile_statements, True, ""
    )
    assert out["status"] == "skipped" and out["reason"] == "not_configured"


async def test_a_transfer_sees_the_far_block_its_email_counterpart_promoted(clean, tmp_path):
    """The counterpart of a transfer must see the block the FAR side promoted.

    `_far_block` looks the far row's outcome up by `row_id`, and the far row
    belongs to another statement by definition. The activity used to hand
    `post_statement` only this statement's slice of the outcomes, so that lookup
    always missed in production: the counterpart's money looked absent from the
    books, was added back to §9.3's comparison, and reverted a statement that
    was perfectly correct — every day, for ever, since it never earns
    `reconciled_at`.

    The existing unit test passes the FULL dict to both calls, so it proves a
    path production never took. This one drives the real activity.
    """
    cfg = _repo(tmp_path)
    # An IMPS the email lane already posted, indexed and in the journal.
    await clean.execute(
        "INSERT INTO finance.journal_index (message_id, mailbox, entity, kind, direction, "
        "amount, currency, payee, payee_key, instrument, occurred_on, parser, source_class, "
        "journal_file) VALUES ('st-imps','st-box','hikmah','transaction','out',100000,'INR',"
        "'Transfer','transfer','axis-9640','2026-07-15','bank_alert','bank','hikmah/2026.journal')"
    )
    (cfg.path / "hikmah" / "2026.journal").write_text(
        "; h\n\n"
        "2026-07-15 ! Transfer\n"
        "    ; msgid: st-imps\n"
        "    ; channel: imps, instrument: axis-9640\n"
        "    equity:transfers          ₹100000.00\n"
        "    assets:bank:axis:9640    ₹-100000.00\n"
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "seed"],
        cwd=cfg.path, check=True,
    )

    axis = await _statement(clean, "axis-9640", "2026-07-01", "2026-07-31", "0", "-100000", 1)
    await _row(
        clean, "axis-9640", "2026-07-15", "out", "100000.00",
        "IMPS/P2A/612345678901/SELF/XXXXXXX1225/HDFCBANKLTD/", axis, "-100000",
    )
    # The receiving statement also holds an ordinary row. Without one its plan
    # has nothing to write, `post_statement` returns before `mutate`, and the
    # closing-balance check never runs at all — so a statement of pure
    # counterparts cannot show this bug even when it is present.
    hdfc = await _statement(clean, "hdfc-1225", "2026-07-01", "2026-07-31", "0", "99500", 2)
    await _row(
        clean, "hdfc-1225", "2026-07-15", "in", "100000.00",
        "IMPS-612345678901-SELF-UTIB-XXXXXXXXXXX9640-IMPS", hdfc, "100000",
    )
    await _row(
        clean, "hdfc-1225", "2026-07-20", "out", "500.00", "ATM WITHDRAWAL", hdfc, "99500",
    )

    out = await ActivityEnvironment().run(_act(clean, cfg).reconcile_statements, True, "")

    by_id = {r["statement"]: r for r in out["results"]}
    assert by_id[axis]["status"] == "posted", out
    # The counterpart must reconcile, not revert. A revert here is the bug.
    assert by_id[hdfc]["status"] == "posted", out
    # And the money is in the books exactly once.
    ledger = (cfg.path / "hikmah" / "2026.journal").read_text() + (
        cfg.path / "personal" / "2026.journal"
    ).read_text()
    assert ledger.count("100000.00") == 2, ledger  # one block, two postings
    assert await clean.fetchval(
        "SELECT through_date FROM finance.reconciled_through WHERE instrument = $1",
        "hdfc-1225",
    ) == date(2026, 7, 31)


class _S:
    """The two fields `_coverage_findings` reads off a stored statement."""

    def __init__(self, instrument, start, end):
        self.instrument, self.period_start, self.period_end = instrument, start, end


def test_coverage_never_reports_an_account_that_has_never_sent_a_statement():
    """`axis-cc-1747`, `icici-143` and `nkgsb-843` are declared, have a Drive
    folder, and have never produced a file. Asking "did one arrive last month?"
    of those opens three problems and three Todoist tasks that no statement can
    ever resolve. Coverage means a bank that STOPPED, which is only a question
    about a bank that started."""
    from aegis.services import statement_findings

    seen = [
        _S("hdfc-1225", date(2026, 7, 1), date(2026, 7, 31)),   # sent last month
        _S("axis-9640", date(2026, 6, 1), date(2026, 6, 30)),   # silent since June
    ]
    out = statements_mod._coverage_findings(
        seen, statement_findings, today=date(2026, 8, 20)
    )
    assert [f["subject"] for f in out] == ["axis-9640"]


def test_coverage_says_nothing_while_the_month_is_still_young():
    """A statement for last month arrives within days of it closing. Asking on
    the 2nd flips every account to missing and resolves it again a week later —
    one Todoist task per account per month, saying only that the calendar
    turned over."""
    from aegis.services import statement_findings

    seen = [_S("axis-9640", date(2026, 6, 1), date(2026, 6, 30))]
    assert statements_mod._coverage_findings(
        seen, statement_findings, today=date(2026, 8, 2)
    ) == []


async def test_the_digest_is_produced_once_a_month_not_once_a_day(clean):
    """#464. The flow ticks daily. `monthly_digest` is §15.4's periodic READ on
    how the lane is doing — a long per-account list whose counts barely move
    between days — so sending it daily teaches the reader to skip it, which is
    the one thing the hub design is trying to avoid.

    The marker is the month, not the day: a tick missed on the 1st still
    produces the report on the 2nd, and two ticks on one day produce one."""
    from aegis.services import statement_findings
    from aegis.services.statement_match import MatchRun

    run = MatchRun(outcomes=(), summaries=(), claimed={})

    first = await statements_mod._due_digest(
        clean, statement_findings, run, today=date(2026, 9, 1)
    )
    assert "Reconciliation digest — 2026-09" in first

    # Same month, later day: the flow ticked again and there is nothing new to
    # report. An empty digest is what stops the flow sending one.
    assert await statements_mod._due_digest(
        clean, statement_findings, run, today=date(2026, 9, 2)
    ) == ""
    assert await statements_mod._due_digest(
        clean, statement_findings, run, today=date(2026, 9, 30)
    ) == ""

    # The month turns over. Note the day: the first tick of October is the 1st
    # here, but a missed tick would make it the 2nd and the report still lands.
    assert "2026-10" in await statements_mod._due_digest(
        clean, statement_findings, run, today=date(2026, 10, 2)
    )


def test_coverage_waits_out_the_accounts_own_cycle_not_just_the_calendar():
    """The live case #463's fix moved rather than removed, caught by checking
    the fix against the real statement table before calling it done.

    `hdfc-1225` bills the 12th to the 11th. The statement covering August is
    `2026-08-12..2026-09-11`, ISSUED on 11 September — but the calendar grace
    opens the question on the 9th, so the account is reported missing on the
    9th and 10th and resolves on the 11th. Every month. That is the same
    monthly false task #463 was filed to remove, two days long instead of
    permanent.

    `_COVERAGE_GRACE_DAYS` waits out the MONTH. This waits out the ACCOUNT,
    which is the thing actually sending, and it works for any billing day.
    """
    from aegis.services import statement_findings

    # 31 days since it last reported: not yet overdue, whatever the month says.
    recent = [_S("hdfc-1225", date(2026, 7, 12), date(2026, 8, 11))]
    assert statements_mod._coverage_findings(
        recent, statement_findings, today=date(2026, 9, 11)
    ) == []

    # Still nothing five weeks later, and now it really has stopped.
    out = statements_mod._coverage_findings(
        recent, statement_findings, today=date(2026, 9, 20)
    )
    assert [f["subject"] for f in out] == ["hdfc-1225"]


def test_coverage_accepts_a_billing_period_that_ends_in_the_following_month():
    """#463. HDFC bills the 5th to the 4th, so August's statement is
    `2026-08-05..2026-09-04` — it covers all but four days of August and ENDS
    in September. Comparing end months reported it missing every month it
    arrived on time, and no statement could ever resolve that. A statement
    covers a month when its period OVERLAPS the month.

    Both alignments are pinned here — the 5th-to-4th shape and the calendar
    shape — because the previous fix got one right by breaking the other. So is
    the other direction: JULY's 5th-to-4th statement overlaps August as well,
    and must not answer for an August that never arrived."""
    from aegis.services import statement_findings

    seen = [
        _S("hdfc-0236", date(2026, 8, 5), date(2026, 9, 4)),    # August's, 5th-to-4th
        _S("axis-9640", date(2026, 8, 1), date(2026, 8, 31)),   # August's, calendar
        _S("hdfc-1225", date(2026, 7, 5), date(2026, 8, 4)),    # JULY's — August never came
        _S("icici-143", date(2026, 6, 1), date(2026, 6, 30)),   # silent since June
    ]
    out = statements_mod._coverage_findings(
        seen, statement_findings, today=date(2026, 9, 20)
    )
    assert [f["subject"] for f in out] == ["hdfc-1225", "icici-143"]
    assert out[0]["payload"]["period"] == "2026-08"


async def test_a_statement_posted_block_is_indexed_so_a_late_receipt_cannot_duplicate_it(
    clean, tmp_path
):
    """§7, §9.2: what the lane posts must reach `finance.journal_index`.

    Without the row the block is invisible to the rest of the money lane. The
    expensive consequence is a double count: a vendor receipt arriving after the
    statement posted the payment finds no counterpart, because
    `journal_index.find_match` requires `journal_file IS NOT NULL`, so it posts
    a SECOND block for money the books already hold — and §9.3 cannot see it,
    because both blocks sit inside the period and the movement still adds up.
    The cheaper consequences: the row is never a matcher candidate again, so its
    account's unmatched count can never fall, and `ledger_reclassify` reads the
    index, so the block cannot be moved by chat.
    """
    cfg = _repo(tmp_path)
    sid = await _statement(clean, "hdfc-1225", "2026-07-01", "2026-07-31", "0", "-500", 1)
    await _row(clean, "hdfc-1225", "2026-07-10", "out", "500.00", "ATM WITHDRAWAL", sid, "-500")

    out = await ActivityEnvironment().run(_act(clean, cfg).reconcile_statements, True, "")
    assert out["posted"] == 1

    row = await clean.fetchrow(
        "SELECT * FROM finance.journal_index WHERE mailbox = 'statement'"
    )
    assert row is not None, "the posted block has no index row"
    assert row["journal_file"], "an index row with no journal_file is not a counterpart"
    assert row["amount"] == Decimal("500.00")
    assert row["direction"] == "out"
    assert row["instrument"] == "hdfc-1225"
    assert row["occurred_on"] == date(2026, 7, 10)
    # The account the poster actually chose, not one recomputed by the caller.
    assert row["account"] == "expenses:fees"


async def test_a_promoted_block_tells_the_index_which_account_paid(clean, tmp_path):
    """#408. A vendor receipt names what you bought, never what paid for it —
    of the twelve amount-bearing NULL-instrument rows in the live index, not
    one of their emails prints a card tail. Promotion already rewrites the
    block's `assets:unknown` posting to the statement's account, so the journal
    knows; the index row was the only thing left saying it did not, and the
    journal is the record while this table is only its index.
    """
    cfg = _repo(tmp_path)
    await clean.execute(
        "INSERT INTO finance.journal_index (message_id, mailbox, entity, kind, direction, "
        "amount, currency, payee, payee_key, occurred_on, parser, source_class, journal_file) "
        "VALUES ('st-apple','st-box','hikmah','transaction','out',219,'INR','Apple','apple',"
        "'2026-07-06','llm','receipt','hikmah/2026.journal')"
    )
    (cfg.path / "hikmah" / "2026.journal").write_text(
        "; h\n\n"
        "2026-07-06 ! Apple\n"
        "    ; msgid: st-apple\n"
        "    ; channel: receipt\n"
        "    expenses:unknown          ₹219.00\n"
        "    assets:unknown           ₹-219.00\n"
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "seed"],
        cwd=cfg.path, check=True,
    )
    sid = await _statement(clean, "axis-9640", "2026-07-01", "2026-07-31", "0", "-219", 1)
    await _row(clean, "axis-9640", "2026-07-06", "out", "219.00", "APPLE SERVICES", sid, "-219")

    out = await ActivityEnvironment().run(_act(clean, cfg).reconcile_statements, True, "")
    assert out["promoted"] == 1, out

    ledger = (cfg.path / "hikmah" / "2026.journal").read_text()
    assert "assets:bank:axis:9640" in ledger, ledger
    assert await clean.fetchval(
        "SELECT instrument FROM finance.journal_index WHERE message_id = 'st-apple'"
    ) == "axis-9640"
