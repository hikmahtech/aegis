"""The read-only money views: balances, bills, unexplained postings, statements
and the trading desk.

Every test runs against a REAL Postgres and, where hledger is installed, a REAL
journal. The point of these endpoints is that a screen and the month close
cannot disagree, and a mocked pool would only ever prove that a mock returns
what it was told to.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.api.routes import money as money_routes
from aegis.config import Settings
from aegis.services import trading_desk as td
from httpx import ASGITransport, AsyncClient

HAS_HLEDGER = shutil.which("hledger") is not None

# Every row this file writes carries one of these prefixes, so the cleanup can
# scope on it. The desk tables are the exception — they carry no free-text key,
# so they are wiped whole, exactly as the desk's own service tests do.
PREFIX = "zzt9-views/"
TASK_PREFIX = "zzt9-views-task-"
INSTRUMENT = "zzt9-bank-0001"
DESK_KEY = "zzt9_desk_problem"

_WIPE = (
    f"DELETE FROM finance.journal_index WHERE message_id LIKE '{PREFIX}%'",
    f"DELETE FROM todoist_tasks WHERE id LIKE '{TASK_PREFIX}%'",
    f"DELETE FROM finance.statement_rows WHERE instrument = '{INSTRUMENT}'",
    f"DELETE FROM finance.statements WHERE instrument = '{INSTRUMENT}'",
    f"DELETE FROM finance.reconciled_through WHERE instrument = '{INSTRUMENT}'",
    "DELETE FROM finance.desk_orders",
    "DELETE FROM finance.desk_plans",
    "DELETE FROM finance.desk_decisions",
    "DELETE FROM finance.desk_prices",
    "DELETE FROM problem_events WHERE problem_id IN "
    "(SELECT id FROM problems WHERE subject_kind = 'trading_desk')",
    "DELETE FROM problems WHERE subject_kind = 'trading_desk'",
)


def _settings(**over) -> Settings:
    base = {
        "database_url": "postgresql://test:test@localhost/test",
        "litellm_url": "https://litellm.test/v1",
        "temporal_ui_url": "https://temporal.test",
        "n8n_ui_url": "https://n8n.test",
        "admin_username": "admin",
        "admin_password": "admin",
        "n8n_webhook_secret": "test-secret",
        "api_key": "test-key",
        "money_hygiene_enabled": True,
    }
    base.update(over)
    return Settings(**base)


def _client(db_pool, **settings_over) -> AsyncClient:
    app = create_app(run_lifespan=False)
    app.state.db_pool = db_pool
    app.dependency_overrides[get_settings] = lambda: _settings(**settings_over)
    # httpx, not TestClient: TestClient's own event loop would InterfaceError
    # on the shared asyncpg pool.
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": "test-key"},
    )


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    for sql in _WIPE:
        await db_pool.execute(sql)
    yield db_pool
    for sql in _WIPE:
        await db_pool.execute(sql)


@pytest_asyncio.fixture(loop_scope="function")
async def client(pool, tmp_path):
    """A client whose books path does NOT exist — a fresh deployment's state,
    and the one the non-hledger tests need."""
    async with _client(pool, books_path=str(tmp_path / "nobooks")) as c:
        yield c


# ----------------------------------------------------------------- the balances


def test_the_total_row_is_not_counted_as_an_account():
    """hledger's csv carries the grand total in the same shape as an account
    row, and its first cell is "Total:".

    An account name never ends in a colon — that would be an empty final
    component — which is the rule that separates the two. A bare `":" in cell`
    test lets "Total:" through and reports the whole portfolio a second time as
    an account called Total. The mixed-commodity cell is passed through
    verbatim: with no rate for one commodity there is no single rupee figure
    that is true, and hledger's own answer is the honest one.
    """
    text = (
        '"account","balance"\n'
        '"assets:bank:axis:9640","₹1,00,000.00"\n'
        '"assets:cash","$-40.00"\n'
        '"Total:","$-40.00, ₹1,00,000.00"\n'
    )

    report = money_routes._balance_report(text)

    assert [r["account"] for r in report["rows"]] == ["assets:bank:axis:9640", "assets:cash"]
    assert report["rows"][0]["balance"] == "₹1,00,000.00"
    assert report["total"] == "$-40.00, ₹1,00,000.00"


async def test_balances_report_a_missing_checkout_instead_of_a_500(client):
    """The books are a git checkout that can be absent, mid-clone or unreadable.

    That is a named failure on this page, not a 500: everything else the money
    screen shows comes from Postgres and is still worth rendering.
    """
    resp = await client.get("/api/admin/money/balances")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["books_ok"] is False
    assert body["error"]
    assert body["standing"] == {"rows": [], "total": None}


def _journal(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "main.journal").write_text(
        "account assets:bank:axis:9640\n"
        "account liabilities:card:axis:1313\n"
        "account expenses:food\n"
        "account income:salary\n"
        "\n"
        f"{date.today().replace(day=1)} Salary\n"
        "    assets:bank:axis:9640    ₹100000.00\n"
        "    income:salary\n"
        "\n"
        f"{date.today().replace(day=1)} Lunch\n"
        "    expenses:food    ₹1234.50\n"
        "    liabilities:card:axis:1313\n"
    )


@pytest.mark.skipif(not HAS_HLEDGER, reason="hledger not installed")
async def test_balances_read_the_real_journal(pool, tmp_path):
    """The standing report is every asset and liability account; the month
    report is income and expenses since the 1st. Both are hledger's own cells."""
    _journal(tmp_path / "books")
    async with _client(pool, books_path=str(tmp_path / "books")) as c:
        resp = await c.get("/api/admin/money/balances")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["books_ok"] is True and body["error"] is None
    standing = {r["account"]: r["balance"] for r in body["standing"]["rows"]}
    assert "assets:bank:axis:9640" in standing
    # hledger's own cell, verbatim — grouping and all. This journal declares
    # no commodity format, so the digits are ungrouped here; a real chart's
    # `commodity` directive is what puts the Indian grouping in.
    assert standing["assets:bank:axis:9640"].startswith("₹")
    assert "100000" in standing["assets:bank:axis:9640"].replace(",", "")
    assert "liabilities:card:axis:1313" in standing
    # "Total:" is the total, never a row.
    assert "Total:" not in standing
    assert body["standing"]["total"]
    month = {r["account"]: r["balance"] for r in body["month"]["rows"]}
    assert "expenses:food" in month
    assert "1234.50" in month["expenses:food"].replace(",", "")
    # Income is a credit, so the ledger writes it negative. The page says so
    # rather than flipping the sign behind the reader's back.
    assert month["income:salary"].startswith("₹-")
    assert body["month_start"] == date.today().replace(day=1).isoformat()


# --------------------------------------------------------------------- the bills


async def _due(pool, suffix, *, due_on, amount="500.00", ref=None, linked=None, kind="due"):
    await pool.execute(
        "INSERT INTO finance.journal_index "
        "(message_id, mailbox, entity, kind, amount, currency, payee, payee_key, due_on, "
        " parser, confidence, todoist_ref, linked_message_id) "
        "VALUES ($1,'personal','personal',$2,$3,'INR','Axis Bank','axis',$4,'test',0.9,$5,$6)",
        f"{PREFIX}{suffix}", kind,
        None if amount is None else Decimal(amount),
        due_on, ref, linked,
    )


async def _task(pool, task_id, *, completed):
    await pool.execute(
        "INSERT INTO todoist_tasks (id, content, labels, source_tag, is_completed, raw) "
        "VALUES ($1, 'Pay the bill', ARRAY['#bill'], '#bill', $2, '{}'::jsonb)",
        task_id, completed,
    )


async def test_dues_use_the_same_open_predicate_as_the_month_close(client, pool):
    """Open means `OPEN_DUE_SQL` — the predicate the month close and the money
    counter already use.

    A ₹0 invoice is not a bill, an undated one can never be matched or be late,
    and a ticked-off one has been dealt with. Quoting a fresh WHERE clause here
    is exactly how four screens come to report four different "still open"
    numbers, so the three excluded shapes are pinned.
    """
    today = date.today()
    await _due(pool, "future", due_on=today + timedelta(days=5), ref="991")
    await _due(pool, "late", due_on=today - timedelta(days=3), ref="992")
    await _due(pool, "zero", due_on=today + timedelta(days=5), amount="0.00", ref="993")
    await _due(pool, "undated", due_on=None, ref="994")
    await _task(pool, f"{TASK_PREFIX}done", completed=True)
    await _due(pool, "ticked", due_on=today + timedelta(days=5), ref=f"{TASK_PREFIX}done")

    resp = await client.get("/api/admin/money/dues")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    mine = [d for d in body["open"] if d["message_id"].startswith(PREFIX)]
    assert [d["message_id"] for d in mine] == [f"{PREFIX}late", f"{PREFIX}future"]
    assert body["overdue_count"] >= 1
    assert any(d["message_id"] == f"{PREFIX}late" for d in mine)
    # A ticked-off bill counts as paid, so it leaves the list — but it is
    # counted, because "I ticked it, did it settle?" is a fair question.
    assert body["ticked_off_count"] >= 1
    # The amount crosses the wire as the ledger's own string; a float would
    # round-trip through binary and disagree on the last paisa.
    assert mine[0]["amount"] == "500.00"


async def test_recently_paid_bills_are_the_ones_a_payment_was_matched_to(client, pool):
    """`linked_message_id` is what says the money moved. A ticked task is not
    evidence about that and must not put a bill in this list."""
    today = date.today()
    await _due(pool, "settled", due_on=today - timedelta(days=10), linked=f"{PREFIX}payment")
    await _due(pool, "stillopen", due_on=today + timedelta(days=2), ref="995")

    body = (await client.get("/api/admin/money/dues")).json()

    paid = [d["message_id"] for d in body["paid_recently"]]
    assert f"{PREFIX}settled" in paid
    assert f"{PREFIX}stillopen" not in paid


# -------------------------------------------------------------- the unexplained


async def test_unknown_totals_count_the_window_not_the_page(client, pool):
    """`totals` is its own aggregate, not a sum over the rows returned.

    The row list is capped and skips a posting the writer refused (it has no
    amount at all), so a total derived from it would be the total of what fits
    on screen — presented as the total. That is the one number here whose whole
    job is to be the real one (issue #391).
    """
    today = date.today()
    await pool.execute(
        "INSERT INTO finance.journal_index "
        "(message_id, mailbox, entity, kind, amount, currency, payee, payee_key, account, "
        " occurred_on, parser, confidence) VALUES "
        "($1,'personal','personal','transaction',$2,'INR','Shop','shop','expenses:unknown',$3,'t',0.5),"
        "($4,'personal','personal','transaction',NULL,'INR','Shop','shop','expenses:unknown',$3,'t',0.5),"
        "($5,'personal','personal','transaction',$6,'USD','Shop','shop','expenses:unknown',$3,'t',0.5)",
        f"{PREFIX}u1", Decimal("100.00"), today - timedelta(days=1),
        f"{PREFIX}u2", f"{PREFIX}u3", Decimal("7.00"),
    )

    body = (await client.get("/api/admin/money/unknowns")).json()

    rows = [r["message_id"] for r in body["rows"]]
    assert f"{PREFIX}u1" in rows and f"{PREFIX}u3" in rows
    assert f"{PREFIX}u2" not in rows  # no amount — nothing to print
    totals = {(t["account"], t["currency"]): t for t in body["totals"]}
    # Grouped by currency as well as account: adding a dollar to a rupee gives
    # a figure that is true of nothing.
    assert totals[("expenses:unknown", "INR")]["count"] == 2  # includes the amount-less row
    assert Decimal(totals[("expenses:unknown", "INR")]["total"]) == Decimal("100.00")
    assert Decimal(totals[("expenses:unknown", "USD")]["total"]) == Decimal("7.00")


# ---------------------------------------------------------------- the statements


def _month_first(offset: int) -> date:
    """The 1st of the month `offset` months before this one."""
    d = date.today().replace(day=1)
    for _ in range(offset):
        d = (d - timedelta(days=1)).replace(day=1)
    return d


def _month_last(first: date) -> date:
    return (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


async def _statement(pool, first: date, *, reconciled: bool, rows: int = 2) -> str:
    last = _month_last(first)
    sid = f"{INSTRUMENT}/{first}..{last}"
    await pool.execute(
        "INSERT INTO finance.statements (statement_id, instrument, period_start, period_end, "
        " opening_balance, closing_balance, file_sha256, rows, reconciled_at) "
        "VALUES ($1,$2,$3,$4,$5,$6,'sha',$7,$8)",
        sid, INSTRUMENT, first, last, Decimal("1000.00"), Decimal("1500.00"), rows,
        datetime.now(UTC) if reconciled else None,
    )
    return sid


async def test_statements_report_coverage_gaps_but_never_this_month(client, pool):
    """A month is covered when a statement's period overlaps it at all, and
    only complete months are checked.

    A card cycle runs the 20th to the 19th and touches two months, so an
    exact-month test would call both missing. And this month's statement has
    not been issued yet, so reporting it missing would be a permanent false
    alarm on a page whose job is to be trusted.
    """
    await _statement(pool, _month_first(3), reconciled=True)
    await _statement(pool, _month_first(1), reconciled=False)
    await pool.execute(
        "INSERT INTO finance.reconciled_through (instrument, through_date, statement_id) "
        "VALUES ($1, $2, $3)",
        INSTRUMENT, _month_last(_month_first(3)), f"{INSTRUMENT}/x",
    )

    body = (await client.get("/api/admin/money/statements")).json()

    acc = next(a for a in body["accounts"] if a["instrument"] == INSTRUMENT)
    assert acc["missing_months"] == [_month_first(2).isoformat()]
    assert date.today().replace(day=1).isoformat() not in acc["missing_months"]
    assert acc["reconciled_through"] == _month_last(_month_first(3)).isoformat()
    assert len(acc["statements"]) == 2
    assert acc["statements"][0]["period_end"] > acc["statements"][1]["period_end"]  # newest first


async def test_statement_lines_are_counted_matched_or_not(client, pool):
    first = _month_first(1)
    sid = await _statement(pool, first, reconciled=False, rows=3)
    for i, matched in enumerate((f"{PREFIX}m", None, None)):
        await pool.execute(
            "INSERT INTO finance.statement_rows (row_id, instrument, occurred_on, narration, "
            " direction, amount, statement_id, file_sha256, matched_msgid) "
            "VALUES ($1,$2,$3,'SHOP','out',$4,$5,'sha',$6)",
            f"{PREFIX}row{i}", INSTRUMENT, first, Decimal("10.00"), sid, matched,
        )

    body = (await client.get("/api/admin/money/statements")).json()

    acc = next(a for a in body["accounts"] if a["instrument"] == INSTRUMENT)
    stmt = next(s for s in acc["statements"] if s["statement_id"] == sid)
    assert (stmt["matched"], stmt["unmatched"]) == (1, 2)
    assert acc["unmatched"] == 2


# ------------------------------------------------------------- the trading desk


async def _price(pool, symbol, day, close):
    await pool.execute(
        "INSERT INTO finance.desk_prices (symbol, date, close, source) "
        "VALUES ($1,$2,$3,'yahoo') ON CONFLICT DO NOTHING",
        symbol, day, close,
    )


async def _plan(pool, day, outcome="orders", findings=None, skipped=None):
    # The pool's jsonb codec applies json.dumps itself, so these are real
    # Python lists — a pre-dumped string would land as a jsonb string scalar.
    await pool.execute(
        "INSERT INTO finance.desk_plans (data_date, mode, outcome, findings, skipped) "
        "VALUES ($1,'paper',$2,$3,$4) ON CONFLICT DO NOTHING",
        day, outcome, findings or [], skipped or [],
    )


async def _order(pool, day, symbol, side, qty, px, *, status="filled", seq=0, costs="10.00"):
    await _plan(pool, day)
    await pool.execute(
        "INSERT INTO finance.desk_orders (mode, data_date, seq, created_day, symbol, asset_class, "
        " side, qty, ref_price, status, fill_date, fill_price, costs, price_source) "
        "VALUES ('paper',$1,$2,$1,$3,'equity',$4,$5,$6,$7,$8,$9,$10,'yahoo')",
        day, seq, symbol, side, qty, px, status,
        day if status == "filled" else None,
        px if status == "filled" else None,
        Decimal(costs) if status == "filled" else None,
    )


async def test_desk_is_all_cash_before_the_first_fill(client, pool):
    body = (await client.get("/api/admin/money/desk")).json()

    assert body["positions"] == []
    assert body["cash"] == body["capital"] == 100_000.0
    assert body["value"] == 100_000.0
    assert body["gain"] == 0.0
    # No fill means no result to score. Reported as nothing, never as a zeroed
    # scorecard that reads like a result.
    assert body["score"] is None


async def test_desk_positions_are_replayed_from_the_fills(client, pool):
    """What the desk holds comes from a replay, never from counting buys minus
    sells: a split leaves more shares than were ever bought, so a count calls a
    still-open position closed and loses it."""
    bought, priced = date.today() - timedelta(days=10), date.today() - timedelta(days=1)
    await _price(pool, "TCS.NS", bought, Decimal("1000.00"))
    await _price(pool, "TCS.NS", priced, Decimal("1100.00"))
    await _order(pool, bought, "TCS", "buy", 10, Decimal("1000.00"))

    body = (await client.get("/api/admin/money/desk")).json()

    assert len(body["positions"]) == 1
    pos = body["positions"][0]
    assert pos["symbol"] == "TCS" and pos["qty"] == 10
    assert pos["avg_cost"] == pytest.approx(1001.0)  # the ₹10 of costs is in the lot
    assert pos["last_close"] == pytest.approx(1100.0)
    assert pos["value"] == pytest.approx(11_000.0)
    assert pos["gain"] == pytest.approx(11_000.0 - 10_010.0)
    assert pos["priced"] is True
    # 100,000 - 10,000 - 10 of costs.
    assert body["cash"] == pytest.approx(89_990.0)
    assert body["value"] == pytest.approx(100_990.0)
    assert body["cash_pct"] == pytest.approx(89_990.0 / 100_990.0, rel=1e-4)


async def test_desk_score_is_the_month_close_s_own_summary(client, pool):
    """The page must not compute a second score. It reports whatever
    `trading_desk.month_summary` says — the identical call the monthly close
    makes — so the two can never disagree."""
    bought = date.today() - timedelta(days=10)
    for day in (bought, date.today() - timedelta(days=1)):
        await _price(pool, "TCS.NS", day, Decimal("1000.00"))
        await _price(pool, "^NSEI", day, Decimal("25000.00"))
        await _price(pool, "SHARIABEES.NS", day, Decimal("400.00"))
    await _order(pool, bought, "TCS", "buy", 10, Decimal("1000.00"))

    body = (await client.get("/api/admin/money/desk")).json()

    first = date.today().replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    assert body["score"] == await td.month_summary(pool, first, nxt)


async def test_desk_pending_orders_are_listed_with_what_they_would_cost(client, pool):
    day = date.today() - timedelta(days=1)
    await _order(pool, day, "GOLDCASE", "buy", 415, Decimal("24.04"), status="pending")

    body = (await client.get("/api/admin/money/desk")).json()

    assert len(body["pending"]) == 1
    order = body["pending"][0]
    assert (order["symbol"], order["side"], order["qty"]) == ("GOLDCASE", "buy", 415)
    assert order["est_value"] == pytest.approx(415 * 24.04)


async def test_desk_complaints_are_keyed_on_the_subject_kind(client, pool):
    """The hub owns problem identity. The page asks it for this subject kind and
    never matches on a title, which would miss a finding the day it is reworded."""
    await pool.execute(
        "INSERT INTO problems (correlation_key, class, subject, subject_kind, title, metadata) "
        "VALUES ($1, 'desk_price_missing', 'TCS', 'trading_desk', 'No price for TCS', "
        "        '{\"description\": \"Neither source has a close.\"}'::jsonb)",
        DESK_KEY,
    )

    body = (await client.get("/api/admin/money/desk")).json()

    assert [p["title"] for p in body["problems"]] == ["No price for TCS"]
    assert body["problems"][0]["description"] == "Neither source has a close."
    assert body["problems"][0]["class"] == "desk_price_missing"


async def test_desk_history_pairs_each_day_with_its_orders_and_its_reasons(client, pool):
    """A plan row exists for every date the desk reached, whatever it decided,
    so the history is also the record of the days it held back — and the reason
    is the day's OWN stored finding, which a later rerun cannot rewrite."""
    held = date.today() - timedelta(days=3)
    traded = date.today() - timedelta(days=2)
    await _plan(
        pool, held, outcome="held_stale",
        findings=[{
            "klass": "desk_decisions_stale", "subject": "decisions", "title": "no decisions",
            "payload": {"description": "ansaar served nothing."},
        }],
    )
    await _plan(pool, traded, outcome="orders", skipped=["SWIGGY"])
    await _order(pool, traded, "TCS", "buy", 5, Decimal("1000.00"))

    body = (await client.get("/api/admin/money/desk/history")).json()

    days = {d["data_date"]: d for d in body["days"]}
    assert days[held.isoformat()]["outcome"] == "held_stale"
    assert days[held.isoformat()]["findings"][0]["payload"]["description"] == (
        "ansaar served nothing."
    )
    assert days[held.isoformat()]["orders"] == []
    assert days[traded.isoformat()]["skipped"] == ["SWIGGY"]
    assert [o["symbol"] for o in days[traded.isoformat()]["orders"]] == ["TCS"]
    assert days[traded.isoformat()]["orders"][0]["status"] == "filled"
    # Newest first, so the page opens on what just happened.
    assert body["days"][0]["data_date"] == traded.isoformat()


def test_hledger_is_reachable_when_the_suite_claims_it_is():
    """A guard on the guard: `HAS_HLEDGER` decides whether the only test that
    exercises the real journal runs at all, so a broken binary must fail here
    rather than silently skip the one test that could catch it."""
    if not HAS_HLEDGER:
        pytest.skip("hledger not installed")
    proc = subprocess.run(["hledger", "--version"], capture_output=True, text=True)
    assert proc.returncode == 0 and "hledger" in proc.stdout
