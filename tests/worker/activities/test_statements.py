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
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from aegis.services import books, statement_post
from aegis.services import statement_findings as sf
from aegis.services.hub import get_problem
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


def test_coverage_does_not_look_while_the_month_is_still_young():
    """A statement for last month arrives within days of it closing. Asking on
    the 2nd flips every account to missing and resolves it again a week later —
    one Todoist task per account per month, saying only that the calendar
    turned over.

    And it says it did not look — None, not `[]` (#491). An empty list means
    "looked, found nothing missing", which is what resolves every open
    `statement_missing` problem."""
    from aegis.services import statement_findings

    seen = [_S("axis-9640", date(2026, 6, 1), date(2026, 6, 30))]
    assert statements_mod._coverage_findings(
        seen, statement_findings, today=date(2026, 8, 2)
    ) is None


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


async def test_a_row_no_rule_places_is_indexed_on_the_account_its_block_names(clean, tmp_path):
    """#481. The poster works the counter account out — the event's own, or the
    rules', or the entity's unknown account — and writes THAT into the block,
    then indexed the event it started from. For a statement row no rule placed
    that event carries no account, so 178 of 298 statement-posted index rows
    said NULL while their blocks said `expenses:unknown`, and every "what is
    still unclassified?" surface keys on `account LIKE '%:unknown'`: the money
    brief, month close and `ledger_add_rule`'s sweep could not see them."""
    cfg = _repo(tmp_path)
    sid = await _statement(clean, "hdfc-1225", "2026-07-01", "2026-07-31", "0", "-120", 1)
    rid = await _row(
        clean, "hdfc-1225", "2026-07-14", "out", "120.00", "POS CORNER STORE", sid, "-120"
    )

    out = await ActivityEnvironment().run(_act(clean, cfg).reconcile_statements, True, "")
    assert out["posted"] == 1, out

    msgid = statement_post.msgid_for(rid)
    text = (cfg.path / "personal" / "2026.journal").read_text()
    span = books.find_block(text, msgid)
    named = statement_post._counter_account(text[span[0]:span[1]])
    assert named == "expenses:unknown", text
    assert await clean.fetchval(
        "SELECT account FROM finance.journal_index WHERE message_id = $1", msgid
    ) == named


async def _indexed(pool, msgid, instrument, day, amount, *, direction="out"):
    """A journal transaction the matcher may offer as a candidate. It names a
    journal file, which `load_candidates` requires of every candidate."""
    await pool.execute(
        "INSERT INTO finance.journal_index (message_id, mailbox, entity, kind, direction, "
        "amount, currency, payee, payee_key, instrument, occurred_on, parser, source_class, "
        "journal_file) VALUES ($1,'st-box','personal','transaction',$2,$3,'INR','Shop','shop',"
        "$4,$5,'bank_alert','bank','personal/2026.journal')",
        msgid, direction, Decimal(amount), instrument, date.fromisoformat(day),
    )


async def _three_verdicts(pool) -> tuple[str, str, str, str]:
    """One hdfc-1225 statement holding each of the matcher's verdicts: a row the
    journal holds once, a row it holds twice (§9.4's ambiguous shape), and a row
    it does not hold at all."""
    sid = await _statement(pool, "hdfc-1225", "2026-07-01", "2026-07-31", "0", "-1374.38", 3)
    await _indexed(pool, "st-one", "hdfc-1225", "2026-07-05", "250.00")
    await _indexed(pool, "st-amb-1", "hdfc-1225", "2026-07-11", "437.19")
    await _indexed(pool, "st-amb-2", "hdfc-1225", "2026-07-12", "437.19")
    matched = await _row(pool, "hdfc-1225", "2026-07-05", "out", "250.00", "UPI SHOP", sid, "-250")
    ambiguous = await _row(
        pool, "hdfc-1225", "2026-07-12", "out", "437.19", "UPI OTHER", sid, "-687.19"
    )
    unmatched = await _row(
        pool, "hdfc-1225", "2026-07-20", "out", "687.19", "SOMETHING", sid, "-1374.38"
    )
    return sid, matched, ambiguous, unmatched


def _spy_on_sweep(monkeypatch) -> list[dict]:
    """Every finding the activity hands the hub. The real sweep still runs."""
    real = sf.sweep
    seen: list[dict] = []

    async def spy(pool, findings, **kw):
        seen.extend(findings)
        return await real(pool, findings, **kw)

    monkeypatch.setattr(sf, "sweep", spy)
    return seen


def _row_findings(seen: list[dict], subject: str) -> list[str]:
    return sorted(
        f["klass"]
        for f in seen
        if f["subject"] == subject and f["klass"] in (sf.UNMATCHED_ROWS, sf.AMBIGUOUS_ROW)
    )


async def test_a_statement_the_lane_will_never_post_raises_no_row_finding(
    clean, tmp_path, monkeypatch
):
    """The lane posts only statements starting on or after `since`, and the
    findings counted every statement — so the 2,339 unmatched rows on
    axis-9640's seven pre-July statements were a task no statement could ever
    close. A row the lane will never post is not work the lane can do."""
    cfg = _repo(tmp_path)
    inst = f"zzold-{uuid.uuid4().hex[:8]}"
    old = await _statement(clean, inst, "2024-05-01", "2024-05-31", "0", "-500", 1)
    await _row(clean, inst, "2024-05-10", "out", "500.00", "OLD PURCHASE", old, "-500")
    seen = _spy_on_sweep(monkeypatch)

    out = await ActivityEnvironment().run(
        _act(clean, cfg).reconcile_statements, True, "2026-07-01"
    )

    assert [r["status"] for r in out["results"]] == ["out_of_scope"]
    assert _row_findings(seen, inst) == []
    # The digest reads the same narrowed run, so the backlog it prints is the
    # backlog the lane can still work.
    assert out["digest"] and old not in out["digest"]


async def test_a_reconciled_statement_raises_no_row_finding(clean, tmp_path, monkeypatch):
    """A reconciled statement passed §9.3: the bank's own printed totals agree
    with the books. An unmatched or ambiguous row left in one is the matcher
    failing to see its own posted entry, or a transfer indexed under the other
    account — hdfc-1225's four live rows were ₹1,00,000 in from the Axis
    account, two ₹1,000 transfers to the kids' accounts and a ₹0.35 SMS fee —
    not money missing from the books.

    Both ways of being reconciled count: on an earlier tick, and on THIS tick,
    whose rows the matcher saw as unmatched a moment before they were posted."""
    cfg = _repo(tmp_path)
    earlier = f"zzdone-{uuid.uuid4().hex[:8]}"
    done = await _statement(clean, earlier, "2026-07-01", "2026-07-31", "0", "-1137.19", 2)
    await clean.execute(
        "UPDATE finance.statements SET reconciled_at = now() WHERE statement_id = $1", done
    )
    await _indexed(clean, "st-amb-1", earlier, "2026-07-11", "437.19")
    await _indexed(clean, "st-amb-2", earlier, "2026-07-12", "437.19")
    await _row(clean, earlier, "2026-07-12", "out", "437.19", "UPI OTHER", done, "-437.19")
    await _row(clean, earlier, "2026-07-20", "out", "700.00", "SOMETHING", done, "-1137.19")
    now = await _statement(clean, "hdfc-1225", "2026-07-01", "2026-07-31", "0", "-500", 1)
    await _row(clean, "hdfc-1225", "2026-07-10", "out", "500.00", "ATM WITHDRAWAL", now, "-500")
    seen = _spy_on_sweep(monkeypatch)

    out = await ActivityEnvironment().run(_act(clean, cfg).reconcile_statements, True, "")

    assert {r["statement"]: r["status"] for r in out["results"]} == {now: "posted"}, out
    assert _row_findings(seen, earlier) == []
    assert _row_findings(seen, "hdfc-1225") == []


async def test_an_unreconciled_statement_in_scope_keeps_its_row_findings(
    clean, tmp_path, monkeypatch
):
    """The narrowing must not overreach. An in-scope statement that has not
    reconciled is exactly where the lane's work is, and its unmatched and
    ambiguous rows are the findings that say so."""
    cfg = _repo(tmp_path)
    await _three_verdicts(clean)
    seen = _spy_on_sweep(monkeypatch)

    await ActivityEnvironment().run(_act(clean, cfg).reconcile_statements, False, "2026-07-01")

    assert _row_findings(seen, "hdfc-1225") == [sf.AMBIGUOUS_ROW, sf.UNMATCHED_ROWS]


async def test_the_matchers_verdict_is_recorded_on_its_row(clean, tmp_path):
    """#470. The matcher decided, the activity handed the decision to the poster,
    and it was gone when the tick ended: `matched_msgid` was NULL on all 2,800
    rows, so "which email did this bank row match?" had no answer anywhere.

    A daily tick over ~2,800 rows that have not changed must write none of
    them, so the second identical run leaves every row's `xmin` where it was."""
    cfg = _repo(tmp_path)
    _, matched, ambiguous, unmatched = await _three_verdicts(clean)
    ids = [matched, ambiguous, unmatched]
    sql = (
        "SELECT row_id, matched_msgid, candidates, skip_reason, xmin::text AS xmin "
        "FROM finance.statement_rows WHERE row_id = ANY($1::text[])"
    )

    await ActivityEnvironment().run(_act(clean, cfg).reconcile_statements, False, "")

    got = {r["row_id"]: r for r in await clean.fetch(sql, ids)}
    assert got[matched]["matched_msgid"] == "st-one"
    assert got[matched]["candidates"] is None
    assert got[ambiguous]["matched_msgid"] is None
    assert got[ambiguous]["candidates"] == ["st-amb-1", "st-amb-2"]
    assert got[ambiguous]["skip_reason"] == "ambiguous"
    assert got[unmatched]["matched_msgid"] is None
    assert got[unmatched]["candidates"] is None

    await ActivityEnvironment().run(_act(clean, cfg).reconcile_statements, False, "")

    again = {r["row_id"]: r["xmin"] for r in await clean.fetch(sql, ids)}
    assert again == {k: v["xmin"] for k, v in got.items()}


async def test_posted_at_marks_only_the_rows_this_lane_wrote(clean, tmp_path):
    """`posted_at` says a block was written for this row (032). Not the
    idempotency ledger — that is the `stmt/<row_id>` msgid inside the block —
    so it marks exactly the rows the poster wrote: not a row that promoted a
    block the email lane wrote, and not one stamped before, whose stamp stands."""
    cfg = _repo(tmp_path)
    await _indexed(clean, "st-one", "hdfc-1225", "2026-07-05", "250.00")
    (cfg.path / "personal" / "2026.journal").write_text(
        "; p\n\n"
        "2026-07-05 ! Shop\n"
        "    ; msgid: st-one\n"
        "    ; channel: upi, instrument: hdfc-1225\n"
        "    expenses:unknown          ₹250.00\n"
        "    assets:bank:hdfc:1225    ₹-250.00\n"
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "seed"],
        cwd=cfg.path, check=True,
    )
    sid = await _statement(clean, "hdfc-1225", "2026-07-01", "2026-07-31", "0", "-870", 3)
    promoted = await _row(
        clean, "hdfc-1225", "2026-07-05", "out", "250.00", "UPI SHOP", sid, "-250"
    )
    posted = await _row(
        clean, "hdfc-1225", "2026-07-10", "out", "500.00", "ATM WITHDRAWAL", sid, "-750"
    )
    stamped = await _row(
        clean, "hdfc-1225", "2026-07-14", "out", "120.00", "POS CORNER STORE", sid, "-870"
    )
    before = datetime(2026, 1, 1, tzinfo=UTC)
    await clean.execute(
        "UPDATE finance.statement_rows SET posted_at = $2 WHERE row_id = $1", stamped, before
    )

    out = await ActivityEnvironment().run(_act(clean, cfg).reconcile_statements, True, "")
    assert (out["posted"], out["promoted"]) == (2, 1), out

    at = {
        r["row_id"]: r["posted_at"]
        for r in await clean.fetch(
            "SELECT row_id, posted_at FROM finance.statement_rows WHERE row_id = ANY($1::text[])",
            [promoted, posted, stamped],
        )
    }
    assert at[promoted] is None
    assert at[posted] is not None and at[posted] > datetime.now(UTC) - timedelta(hours=1)
    assert at[stamped] == before


def _on(monkeypatch, day: date) -> None:
    """Stand the activity on `day`: its `date.today()` answers `day`. Threading
    a clock through the activity's arguments would change the schedule's
    payload for the sake of a test."""

    class _Day(date):
        @classmethod
        def today(cls):
            return day

    monkeypatch.setattr(statements_mod, "date", _Day)


async def _open_problems(pool, *klasses: str) -> list[str]:
    """One live money problem per class, each on an account of its own. ONE
    sweep opens them all: a second would resolve the first one's problems."""
    subjects = [f"zzacct-{uuid.uuid4().hex[:8]}" for _ in klasses]
    out = await sf.sweep(
        pool,
        [sf.finding(k, s, f"{k} on {s}") for k, s in zip(klasses, subjects, strict=True)],
        kinds=(sf.INSTRUMENT,),
        project=False,
    )
    ids = {f["subject"]: f["problem_id"] for f in out[sf.INSTRUMENT]["fresh"]}
    return [ids[s] for s in subjects]


async def test_a_tick_inside_the_grace_window_leaves_statement_missing_open(
    clean, tmp_path, monkeypatch
):
    """#491. Coverage does not look for the first `_COVERAGE_GRACE_DAYS` of a
    month, and the sweep was told it had: every open "no statement arrived"
    problem was resolved on the 1st because none was found, and a statement
    that really was missing got a NEW task on the 9th, every month. The classes
    that were evaluated on the same tick still recover."""
    cfg = _repo(tmp_path)
    missing, unmatched = await _open_problems(clean, sf.STATEMENT_MISSING, sf.UNMATCHED_ROWS)
    sid = await _statement(clean, "hdfc-1225", "2026-07-01", "2026-07-31", "0", "-500", 1)
    await _row(clean, "hdfc-1225", "2026-07-10", "out", "500.00", "ATM WITHDRAWAL", sid, "-500")
    _on(monkeypatch, date(2026, 10, 3))

    await ActivityEnvironment().run(_act(clean, cfg).reconcile_statements, False, "")

    assert (await get_problem(clean, missing))["status"] == "open"
    assert (await get_problem(clean, unmatched))["status"] == "resolved"


async def test_a_tick_past_the_grace_window_resolves_what_coverage_no_longer_finds(
    clean, tmp_path, monkeypatch
):
    """The other half. Once coverage really looks, a `statement_missing` it does
    not find again is over — which is how the problem closes the day the
    statement lands."""
    cfg = _repo(tmp_path)
    (missing,) = await _open_problems(clean, sf.STATEMENT_MISSING)
    sid = await _statement(clean, "hdfc-1225", "2026-07-01", "2026-07-31", "0", "-500", 1)
    await _row(clean, "hdfc-1225", "2026-07-10", "out", "500.00", "ATM WITHDRAWAL", sid, "-500")
    _on(monkeypatch, date(2026, 10, 12))

    await ActivityEnvironment().run(_act(clean, cfg).reconcile_statements, False, "")

    assert (await get_problem(clean, missing))["status"] == "resolved"
