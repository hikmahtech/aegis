"""build_money_brief / build_month_close / refresh_fx_prices against a temp books repo."""

from __future__ import annotations

import shutil
import subprocess
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis.api.models.money import MoneyEvent
from aegis.services import books
from aegis.services import journal_index as ji
from aegis_worker.activities.money import (
    MoneyActivities,
    amount_from_cell,
    commodity_of,
    parse_hledger_csv,
    split_amount_cell,
    unconverted_commodities,
)
from temporalio.testing import ActivityEnvironment

HAS_HLEDGER = shutil.which("hledger") is not None and shutil.which("git") is not None

# £ and € are declared because `refresh_fx_prices` writes a P line for each
# pair it gets a quote for, and `hledger check --strict` rejects a price in an
# undeclared commodity — which reverts the whole write, not just that line.
ACCOUNTS = """commodity ₹ 1,00,000.00
commodity $ 1000.00
commodity £ 1000.00
commodity € 1000.00
account assets:bank:hdfc:1225
account assets:unknown
account liabilities:card:axis:1313
account expenses:unknown
account expenses:saas
account expenses:groceries
account expenses:hikmah:unknown
account expenses:hikmah:infra
account income:unknown
account income:hikmah:stockopedia
account income:hikmah:other
account equity:transfers
"""


def _repo(
    tmp_path: Path,
    today: date,
    recurring_from: date | None = None,
    prices: str = "P 2026-09-01 $ ₹84.00\n",
    recurring: str | None = None,
) -> books.BooksConfig:
    """A books checkout anchored on `today`.

    `recurring_from` is the periodic rule's start date. It defaults to the
    first of `today`'s month, but every forecast assertion pins it explicitly:
    hledger generates a `monthly` rule only on its own anchor day, so a rule
    left on the 1st falls outside a "next fortnight" window for most of the
    month and the test would pass or fail by the calendar.

    `prices` is the whole of `prices.journal`. Pass `""` for the books that
    have never had a successful FX refresh — the state where `-X ₹` cannot
    convert and hledger reports two commodities in one cell.
    """
    root = tmp_path / "books"
    (root / "personal").mkdir(parents=True)
    (root / "hikmah").mkdir()
    (root / "accounts.journal").write_text(ACCOUNTS)
    (root / "prices.journal").write_text(prices)
    (root / "recurring.journal").write_text(
        recurring
        if recurring is not None
        else (
            f"~ monthly from {(recurring_from or today.replace(day=1)).isoformat()}  Apple iCloud+\n"
            "    expenses:saas                 ₹219.00\n    liabilities:card:axis:1313\n"
        )
    )
    (root / "personal" / f"{today.year}.journal").write_text("; p\n")
    (root / "hikmah" / f"{today.year}.journal").write_text("; h\n")
    (root / "main.journal").write_text(
        f"include accounts.journal\ninclude prices.journal\ninclude personal/{today.year}.journal\n"
        f"include hikmah/{today.year}.journal\ninclude recurring.journal\n"
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
    await db_pool.execute("DELETE FROM finance.journal_index WHERE mailbox = 'brief-t'")
    yield
    await db_pool.execute("DELETE FROM finance.journal_index WHERE mailbox = 'brief-t'")


def _ev(**kw) -> MoneyEvent:
    base = {
        "kind": "transaction", "direction": "out", "amount": Decimal("10"), "currency": "INR",
        "payee": "Shop", "payee_key": "shop", "channel": "upi", "instrument": "hdfc-1225",
        "occurred_on": date.today(), "entity": "personal", "account": "expenses:unknown",
        "parser": "hdfc_upi", "source_class": "bank",
    }
    return MoneyEvent(**{**base, **kw})


def _act(db_pool, cfg, finance=None) -> MoneyActivities:
    return MoneyActivities(
        db_pool=db_pool, llm=None, delivery=None, books_cfg=cfg, finance=finance
    )


def _mine(rows: list[dict]) -> list[dict]:
    """Only this file's rows, order preserved.

    The index queries are deliberately table-wide — that is right in
    production — so a sibling test file that leaks a row would otherwise fail
    this one for a reason that has nothing to do with the code under test.
    Filtering keeps the assertions exact instead of loosening them to `in`.
    """
    return [r for r in rows if str(r["msgid"]).startswith("brief-t/")]


def test_csv_helpers():
    rows = parse_hledger_csv('"account","balance"\n"expenses:saas","₹ 1,234.56"\n"total","₹ 1,234.56"\n')
    assert rows[1] == ["expenses:saas", "₹ 1,234.56"]
    assert amount_from_cell("₹ 1,234.56") == Decimal("1234.56")
    assert amount_from_cell("₹ -140.00") == Decimal("-140.00")
    assert amount_from_cell("0") == Decimal("0") and amount_from_cell("") == Decimal("0")


def test_a_mixed_commodity_cell_never_reports_a_foreign_amount_as_rupees():
    """The exact cell hledger writes when `-X ₹` has no price for `$`.

    Reading the first number here is how ₹300 of real spend gets reported as
    "50.00" — right-looking, wrong commodity, no error anywhere.
    """
    mixed = "$ 50.00, ₹ 300.00"
    assert split_amount_cell(mixed) == ["$ 50.00", "₹ 300.00"]
    assert amount_from_cell(mixed) == Decimal("300.00")
    assert unconverted_commodities(mixed) == ["$"]
    # Order in the cell must not matter, and a whole cell in one foreign
    # commodity contributes nothing rather than its own number.
    assert amount_from_cell("₹ 300.00, $ 50.00") == Decimal("300.00")
    assert amount_from_cell("$ 50.00") == Decimal("0")
    assert unconverted_commodities("$ 50.00") == ["$"]
    # A digit-group comma has no space after it, so it is never a part break.
    assert split_amount_cell("₹ 1,00,000.00") == ["₹ 1,00,000.00"]
    assert amount_from_cell("₹ 1,00,000.00") == Decimal("100000.00")
    # Fully converted, and the bare zero, are not "stale".
    assert unconverted_commodities("₹ 300.00") == [] and unconverted_commodities("0") == []
    assert commodity_of("$ 50.00") == "$" and commodity_of("50.00 CHF") == "CHF"
    assert commodity_of("0") == ""


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_build_money_brief_reads_books_and_index(db_pool, tmp_path):
    today = date.today()
    cfg = _repo(tmp_path, today, recurring_from=today)
    act = _act(db_pool, cfg)
    # `low_confidence` is a table-wide count with nothing to scope it by, so it
    # is asserted as a delta: a sibling test file that leaks a row must not be
    # able to fail this one for an unrelated reason.
    baseline = await ActivityEnvironment().run(act.build_money_brief, 7)
    await books.post_event(
        _ev(amount=Decimal("6000"), payee="Unknown Big", payee_key="unknown big"), "brief-t/a", cfg)
    await books.post_event(
        _ev(amount=Decimal("250"), payee="Grocer", payee_key="grocer",
            account="expenses:groceries"), "brief-t/b", cfg)
    await books.post_event(
        _ev(amount=Decimal("1000"), direction="in", payee="Stockopedia", payee_key="stockopedia",
            entity="hikmah", account="income:hikmah:stockopedia", instrument=None), "brief-t/c", cfg)
    await ji.upsert(db_pool, "brief-t/a", "brief-t",
                    _ev(amount=Decimal("6000"), payee="Unknown Big", payee_key="unknown big"),
                    journal_file="x")
    await ji.upsert(db_pool, "brief-t/d", "brief-t",
                    _ev(kind="due", due_on=today, payee="Axis card", payee_key="axis card",
                        amount=Decimal("99"), channel="statement"), todoist_ref="t1")
    # A low-confidence row that is NOT an unknown account: `low_confidence` and
    # `unknowns` are separate signals and must not be able to stand in for
    # each other.
    await ji.upsert(db_pool, "brief-t/e", "brief-t",
                    _ev(parser="llm", confidence=0.4, payee="Vague", payee_key="vague",
                        account="expenses:saas"))
    # A due that has already been paid: it leaves `dues` and appears in
    # `closed_dues`.
    await ji.upsert(db_pool, "brief-t/f", "brief-t",
                    _ev(kind="due", due_on=today - timedelta(days=2), payee="Paid bill",
                        payee_key="paid bill", amount=Decimal("77"), channel="bill"),
                    linked="brief-t/b", todoist_ref="t2")
    # Two more unknowns that pin the ordering and the "large" cut: the biggest
    # number in the list is a foreign-currency one, so a `large_unexplained`
    # that ignored `currency` would flag ₹9000 that was never spent.
    await ji.upsert(db_pool, "brief-t/g", "brief-t",
                    _ev(amount=Decimal("20"), payee="Tiny", payee_key="tiny"))
    await ji.upsert(db_pool, "brief-t/h", "brief-t",
                    _ev(amount=Decimal("9000"), currency="USD", payee="Dollars",
                        payee_key="dollars"))

    brief = await ActivityEnvironment().run(act.build_money_brief, 7)
    unknowns = _mine(brief["unknowns"])
    dues = _mine(brief["dues"])

    assert brief["books_ok"] is True and brief["as_of"] == today.isoformat()
    assert brief["since"] == (today - timedelta(days=7)).isoformat()
    assert Decimal(brief["entities"]["personal"]["expenses"]) == Decimal("6250.00")
    assert Decimal(brief["entities"]["personal"]["income"]) == Decimal("0")
    assert Decimal(brief["entities"]["hikmah"]["income"]) == Decimal("-1000.00")
    assert Decimal(brief["entities"]["hikmah"]["expenses"]) == Decimal("0")
    assert brief["fx_stale"] is False and brief["fx_unconverted"] == []
    assert [r["account"] for r in brief["by_account"]] == [
        "expenses:unknown", "expenses:groceries", "income:hikmah"]
    assert [p["payee"] for p in brief["top_payees"]] == ["Unknown Big", "Grocer"]
    assert amount_from_cell(brief["top_payees"][0]["amount"]) == Decimal("6000.00")
    assert [u["msgid"] for u in unknowns] == ["brief-t/h", "brief-t/a", "brief-t/g"]
    assert unknowns[1]["amount"] == "6000.00"
    assert unknowns[1]["occurred_on"] == today.isoformat()
    assert unknowns[1]["channel"] == "upi"
    assert [u["msgid"] for u in _mine(brief["large_unexplained"])] == ["brief-t/a"]
    assert [d["msgid"] for d in dues] == ["brief-t/d"]
    assert dues[0]["todoist_ref"] == "t1" and dues[0]["kind"] == "due"
    assert [c["msgid"] for c in _mine(brief["closed_dues"])] == ["brief-t/f"]
    assert [row["description"] for row in brief["forecast"]] == ["Apple iCloud+"]
    assert amount_from_cell(brief["forecast"][0]["amount"]) == Decimal("219.00")
    assert brief["forecast"][0]["date"] == today.isoformat()
    assert brief["low_confidence"] == baseline["low_confidence"] + 1
    assert brief["unpushed"] == 0
    assert "expenses:groceries" in brief["bal_text"]


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_money_brief_says_so_when_a_rate_is_missing_instead_of_lying(db_pool, tmp_path):
    """No `$` price ⇒ `-X ₹` leaves it alone and hledger reports BOTH
    commodities in one cell. The rupee subtotal must be the rupee part, and the
    brief must carry the fact that something could not be converted.

    Reachable on a fresh books repo, or any week the quote provider is down.
    """
    today = date.today()
    cfg = _repo(tmp_path, today, prices="")
    await books.post_event(
        _ev(amount=Decimal("300"), account="expenses:saas", payee="Shop", payee_key="shop"),
        "brief-t/r", cfg)
    await books.post_event(
        _ev(amount=Decimal("50"), currency="USD", account="expenses:saas", payee="Vendor",
            payee_key="vendor"), "brief-t/s", cfg)

    brief = await ActivityEnvironment().run(_act(db_pool, cfg).build_money_brief, 7)

    assert brief["books_ok"] is True
    assert [r["account"] for r in brief["by_account"]] == ["expenses:saas"]
    assert brief["by_account"][0]["balance"] == "$ 50.00, ₹ 300.00"
    # 300, never 50: the first number in that cell is dollars.
    assert Decimal(brief["entities"]["personal"]["expenses"]) == Decimal("300.00")
    assert brief["fx_stale"] is True and brief["fx_unconverted"] == ["$"]


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_month_close_says_so_when_a_rate_is_missing(db_pool, tmp_path):
    """Same trap on the close, where `is_rows` and `recurring_total` read the
    same mixed cells."""
    prev_last = date.today().replace(day=1) - timedelta(days=1)
    cfg = _repo(tmp_path, prev_last, prices="")
    await books.post_event(
        _ev(amount=Decimal("300"), occurred_on=prev_last, account="expenses:saas", payee="Shop",
            payee_key="shop"), "brief-t/t", cfg)
    await books.post_event(
        _ev(amount=Decimal("50"), currency="USD", occurred_on=prev_last, account="expenses:saas",
            payee="Vendor", payee_key="vendor"), "brief-t/u", cfg)

    close = await ActivityEnvironment().run(_act(db_pool, cfg).build_month_close)

    assert close["books_ok"] is True
    assert [r["account"] for r in close["is_rows"]] == ["expenses:saas"]
    assert close["is_rows"][0]["month"] == "$ 50.00, ₹ 300.00"
    assert amount_from_cell(close["is_rows"][0]["month"]) == Decimal("300.00")
    assert close["fx_stale"] is True and close["fx_unconverted"] == ["$"]


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_a_foreign_forecast_marks_the_brief_stale_on_its_own(db_pool, tmp_path):
    """The forecast is the one place a commodity can appear that the balance
    report never sees: a periodic rule bills in a currency nothing has spent
    yet. There is no real transaction here at all, so only the forecast sweep
    can raise the flag."""
    today = date.today()
    cfg = _repo(tmp_path, today, prices="", recurring=(
        f"~ monthly from {today.isoformat()}  Foreign Sub\n"
        "    expenses:saas                 $9.99\n    liabilities:card:axis:1313\n"
    ))
    brief = await ActivityEnvironment().run(_act(db_pool, cfg).build_money_brief, 7)
    assert brief["books_ok"] is True and brief["by_account"] == []
    assert [r["amount"] for r in brief["forecast"]] == ["$ 9.99"]
    assert brief["fx_stale"] is True and brief["fx_unconverted"] == ["$"]


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_a_foreign_recurring_charge_marks_the_close_stale_and_counts_as_zero(
    db_pool, tmp_path
):
    """Same for the close: `recurring_total` is rupees, so a $9.99 rule adds
    nothing to it — and the reader has to be told that is why."""
    prev_last = date.today().replace(day=1) - timedelta(days=1)
    cfg = _repo(tmp_path, prev_last, prices="", recurring=(
        f"~ monthly from {prev_last.replace(day=1).isoformat()}  Foreign Sub\n"
        "    expenses:saas                 $9.99\n    liabilities:card:axis:1313\n"
    ))
    close = await ActivityEnvironment().run(_act(db_pool, cfg).build_month_close)
    assert close["books_ok"] is True and close["is_rows"] == []
    assert close["recurring_total"] == "0.00"
    assert close["fx_stale"] is True and close["fx_unconverted"] == ["$"]


@pytest.mark.asyncio
async def test_an_unknown_with_no_amount_does_not_take_the_whole_brief_down(db_pool, tmp_path):
    """`post_money_event` indexes a transaction the writer refused, so a row
    with a NULL amount and `account='expenses:unknown'` is really written.
    `Decimal(str(None))` raised `InvalidOperation` — not a `BooksError` — and
    the weekly brief then failed on every retry with nothing saying why.
    """
    act = _act(db_pool, books.BooksConfig(path=tmp_path / "none"))
    await ji.upsert(db_pool, "brief-t/nul", "brief-t",
                    _ev(amount=None, currency="INR", payee="Amountless", payee_key="amountless"))
    await ji.upsert(db_pool, "brief-t/ok", "brief-t",
                    _ev(amount=Decimal("7000"), payee="Real", payee_key="real"))

    brief = await ActivityEnvironment().run(act.build_money_brief, 7)

    assert [u["msgid"] for u in _mine(brief["unknowns"])] == ["brief-t/ok"]
    assert [u["msgid"] for u in _mine(brief["large_unexplained"])] == ["brief-t/ok"]


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_money_brief_forecast_reaches_the_far_edge_of_the_window(db_pool, tmp_path):
    """hledger's `--forecast=A..B` excludes B, so the window has to end one day
    past the last day the brief claims to cover — otherwise a charge exactly a
    fortnight out is silently missing from the fortnight's forecast."""
    today = date.today()
    edge = today + timedelta(days=14)
    cfg = _repo(tmp_path, today, recurring_from=edge)
    brief = await ActivityEnvironment().run(_act(db_pool, cfg).build_money_brief, 7)
    assert brief["books_ok"] is True
    assert [row["date"] for row in brief["forecast"]] == [edge.isoformat()]


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_money_brief_counts_commits_the_books_have_not_pushed(db_pool, tmp_path):
    """`unpushed` is the "the journal is only on this box" warning, and its
    default is also 0 — so it is only worth anything if a real un-pushed
    commit shows up as one."""
    cfg = _repo(tmp_path, date.today())
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=cfg.path, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "HEAD"], cwd=cfg.path, check=True)
    (cfg.path / "personal" / "notes.md").write_text("local only\n")
    subprocess.run([*git, "add", "-A"], cwd=cfg.path, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "local"], cwd=cfg.path, check=True)

    brief = await ActivityEnvironment().run(_act(db_pool, cfg).build_money_brief, 7)
    assert brief["books_ok"] is True and brief["unpushed"] == 1


@pytest.mark.asyncio
async def test_build_money_brief_without_books_still_reports_index(db_pool, tmp_path):
    act = _act(db_pool, books.BooksConfig(path=tmp_path / "none"))
    await ji.upsert(db_pool, "brief-t/z", "brief-t",
                    _ev(kind="due", due_on=date.today(), payee="X", payee_key="x"),
                    todoist_ref="t")
    brief = await ActivityEnvironment().run(act.build_money_brief, 7)
    assert brief["books_ok"] is False and brief["bal_text"] == ""
    assert brief["by_account"] == [] and brief["forecast"] == []
    assert brief["entities"]["personal"] == {"income": "0", "expenses": "0"}
    assert brief["fx_stale"] is False and brief["fx_unconverted"] == []
    assert [d["msgid"] for d in _mine(brief["dues"])] == ["brief-t/z"]
    assert _mine(brief["unknowns"]) == [] and _mine(brief["large_unexplained"]) == []
    assert _mine(brief["closed_dues"]) == []


@pytest.mark.asyncio
async def test_no_books_config_at_all_degrades_instead_of_crashing(db_pool):
    """`books_cfg=None` is the half-configured worker, and it reaches hledger's
    runner as an attribute error rather than a `BooksError` — so it is turned
    into one here instead of taking the whole activity down."""
    act = _act(db_pool, None)
    brief = await ActivityEnvironment().run(act.build_money_brief, 7)
    assert brief["books_ok"] is False and brief["bal_text"] == ""
    close = await ActivityEnvironment().run(act.build_month_close)
    assert close["books_ok"] is False and close["is_text"] == ""
    assert close["month"] == (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_build_month_close(db_pool, tmp_path):
    today = date.today()
    first = today.replace(day=1)
    prev_last = first - timedelta(days=1)
    cfg = _repo(tmp_path, prev_last)
    act = _act(db_pool, cfg)
    # The three index counts are table-wide, and a sibling file really does
    # insert a due inside this window (`test_capture_due.py` uses a hardcoded
    # 2026-08-07). Assert the movement this test causes, not the absolute.
    baseline = await ActivityEnvironment().run(act.build_month_close)
    await books.post_event(
        _ev(amount=Decimal("300"), occurred_on=prev_last, account="expenses:saas", payee="Saas",
            payee_key="saas"), "brief-t/m", cfg)
    await ji.upsert(db_pool, "brief-t/m", "brief-t",
                    _ev(amount=Decimal("300"), occurred_on=prev_last, account="expenses:saas",
                        payee="Saas", payee_key="saas"), journal_file="x")
    await ji.upsert(db_pool, "brief-t/n", "brief-t",
                    _ev(amount=Decimal("40"), occurred_on=prev_last, payee="Mystery",
                        payee_key="mystery"), journal_file="x")
    await ji.upsert(db_pool, "brief-t/o", "brief-t",
                    _ev(kind="due", due_on=prev_last, payee="Bill", payee_key="bill",
                        amount=Decimal("55"), channel="bill"), todoist_ref="t3")
    await ji.upsert(db_pool, "brief-t/p", "brief-t",
                    _ev(kind="due", due_on=prev_last, payee="Settled", payee_key="settled",
                        amount=Decimal("66"), channel="bill"), linked="brief-t/m", todoist_ref="t4")
    # A second open due so paid and open cannot be swapped without noticing.
    await ji.upsert(db_pool, "brief-t/q", "brief-t",
                    _ev(kind="failed", due_on=prev_last, payee="Bounced", payee_key="bounced",
                        amount=Decimal("12"), channel="bill"), todoist_ref="t5")

    close = await ActivityEnvironment().run(act.build_month_close)

    assert close["books_ok"] is True and close["month"] == prev_last.strftime("%Y-%m")
    assert "expenses:saas" in close["is_text"] and "Balance Sheet" in close["bs_text"]
    assert [r["account"] for r in close["is_rows"]] == ["expenses:saas"]
    assert amount_from_cell(close["is_rows"][0]["month"]) == Decimal("300.00")
    assert amount_from_cell(close["is_rows"][0]["prev"]) == Decimal("0")
    assert close["recurring_total"] == "219.00"
    assert close["fx_stale"] is False and close["fx_unconverted"] == []
    assert close["unknown_count"] == baseline["unknown_count"] + 1
    assert close["dues_paid"] == baseline["dues_paid"] + 1
    assert close["dues_open"] == baseline["dues_open"] + 2


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_month_close_forecast_covers_the_last_day_of_the_month(db_pool, tmp_path):
    """Same exclusive-end trap as the brief: a charge on the 31st belongs to
    the month being closed, so the forecast window has to end on the 1st of the
    NEXT month, not on the month's own last day."""
    prev_last = date.today().replace(day=1) - timedelta(days=1)
    cfg = _repo(tmp_path, prev_last, recurring_from=prev_last)
    close = await ActivityEnvironment().run(_act(db_pool, cfg).build_month_close)
    assert close["books_ok"] is True
    assert close["recurring_total"] == "219.00"


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_refresh_fx_prices_appends_p_lines(db_pool, tmp_path):
    cfg = _repo(tmp_path, date.today())
    finance = AsyncMock()
    finance.get_quotes = AsyncMock(return_value=[
        {"symbol": "USDINR=X", "price": 84.1},
        {"symbol": "GBPINR=X", "price": 106.25},
        {"symbol": "EURINR=X", "error": "timeout"},
    ])
    act = _act(db_pool, cfg, finance=finance)
    out = await ActivityEnvironment().run(act.refresh_fx_prices)
    assert out["written"] == 2 and out["errors"] == ["EURINR=X: timeout"]
    finance.get_quotes.assert_awaited_once_with(["USDINR=X", "GBPINR=X", "EURINR=X"])
    text = (cfg.path / "prices.journal").read_text()
    assert f"P {date.today().isoformat()} $ ₹84.10\n" in text
    assert f"P {date.today().isoformat()} £ ₹106.25\n" in text
    act_none = _act(db_pool, cfg, finance=None)
    assert (await ActivityEnvironment().run(act_none.refresh_fx_prices)) == {
        "written": 0, "errors": ["disabled"]}


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_refresh_fx_prices_never_raises_when_the_provider_fails(db_pool, tmp_path):
    cfg = _repo(tmp_path, date.today())
    finance = AsyncMock()
    finance.get_quotes = AsyncMock(side_effect=RuntimeError("upstream 503"))
    out = await ActivityEnvironment().run(_act(db_pool, cfg, finance=finance).refresh_fx_prices)
    assert out["written"] == 0 and out["errors"] == ["quotes: upstream 503"]
    assert (cfg.path / "prices.journal").read_text() == "P 2026-09-01 $ ₹84.00\n"


@pytest.mark.asyncio
async def test_refresh_fx_prices_is_disabled_without_a_books_config(db_pool):
    """`books_cfg=None` with a working quote provider. Without this half of the
    guard `append_prices(lines, None)` raises `AttributeError` on `cfg.path`
    inside `_FileLock.__enter__` — not a `BooksError` — and the activity dies.
    The quotes are not even fetched: there is nowhere to put them.
    """
    finance = AsyncMock()
    finance.get_quotes = AsyncMock(return_value=[{"symbol": "USDINR=X", "price": 84.1}])
    act = _act(db_pool, None, finance=finance)
    assert (await ActivityEnvironment().run(act.refresh_fx_prices)) == {
        "written": 0, "errors": ["disabled"]}
    finance.get_quotes.assert_not_awaited()


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")
@pytest.mark.asyncio
async def test_refresh_fx_prices_survives_a_books_error_it_was_not_told_about(
    db_pool, tmp_path, monkeypatch
):
    """`books.py` does not wrap everything in `BooksError`: `_FileLock` takes
    the flock BEFORE `_write_sync`'s try block, so a permission or disk error
    on `.aegis.lock` escapes as a bare `OSError`. The next task calls this as
    best-effort, so it has to come back as a value, not an exception.
    """
    cfg = _repo(tmp_path, date.today())
    finance = AsyncMock()
    finance.get_quotes = AsyncMock(return_value=[{"symbol": "USDINR=X", "price": 84.1}])
    monkeypatch.setattr(
        "aegis.services.books.append_prices",
        AsyncMock(side_effect=PermissionError("[Errno 13] .aegis.lock")),
    )
    out = await ActivityEnvironment().run(_act(db_pool, cfg, finance=finance).refresh_fx_prices)
    assert out["written"] == 0
    assert out["errors"] == ["books: [Errno 13] .aegis.lock"]
    assert (cfg.path / "prices.journal").read_text() == "P 2026-09-01 $ ₹84.00\n"
