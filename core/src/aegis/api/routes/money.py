"""Admin endpoints for Maou's money: the books, the bills, the statements and
the trading desk.

Everything here is READ-ONLY apart from the one pre-existing flow trigger. The
desk holds real paper positions and the journal is the owner's real accounting,
so the page can show and cannot act.

Two rules run through the whole module:

* **A number is reported by whoever already computes it.** The balances are
  hledger's own rendered cells, passed through verbatim; the desk's score is
  `trading_desk.month_summary`, the same call the monthly close makes; the
  desk's positions come from `desk_math.replay`, the same replay the daily run
  uses to decide what it holds. Nothing here re-derives money, because a second
  implementation of a figure is how two screens start disagreeing.
* **A section that fails renders as a named failure, not a 500.** The books
  live in a git checkout that may be missing, mid-clone or unreadable, and a
  broken `hledger` must not take the bills, the statements and the desk down
  with it.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from temporalio.client import Client as TemporalClient

from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.api.routes._flow_trigger import require_temporal_client, start_named_workflow
from aegis.config import Settings
from aegis.services import books, desk_math, trading_desk
from aegis.services.journal_index import OPEN_DUE_SQL, TICKED_OFF_SQL
from aegis.services.money_format import currency_symbol

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/money",
    tags=["money"],
    dependencies=[Depends(verify_auth)],
)


_FLOW_NAMES = {
    "money_brief": "MoneyBriefFlow",
    "month_close": "MonthCloseFlow",
    "receipt_scan": "ReceiptIngestFlow",
}

# How far back an unexplained posting still counts as something to fix. The
# monthly close scopes the same count to its month; the page is a standing
# queue, so it takes a rolling window instead.
_UNKNOWN_DAYS = 60

_EVENT_LIMIT = 100
_REPORTS_REL = "reports/monthly"

# The columns the admin page renders, in the order the spec lists them.
_EVENT_COLUMNS = (
    "message_id", "mailbox", "entity", "kind", "direction", "amount", "currency",
    "payee", "account", "channel", "instrument", "occurred_on", "due_on", "parser",
    "confidence", "source_class", "journal_file", "linked_message_id", "todoist_ref",
)


async def _start_workflow(flow: str, cfg: dict, temporal_client: TemporalClient):
    return await start_named_workflow(flow, cfg, temporal_client, _FLOW_NAMES)


def _event(row) -> dict:
    """One index row, JSON-safe.

    `amount` leaves as the string Postgres stored, never a float: the journal
    is the record and this is only its index, so the page must reproduce the
    ledger's digits exactly rather than a binary approximation of them. A row
    the writer refused has no amount at all, and that stays `None` — `str()`
    on it would put the literal "None" in front of a currency symbol.
    """
    out = {k: row[k] for k in _EVENT_COLUMNS}
    out["amount"] = None if row["amount"] is None else str(row["amount"])
    for key in ("occurred_on", "due_on"):
        out[key] = out[key].isoformat() if out[key] else None
    out["confidence"] = None if row["confidence"] is None else float(row["confidence"])
    return out


@router.get("/state")
async def money_state(request: Request, settings: Settings = Depends(get_settings)) -> dict:
    """The books index: recent events, the two review queues, and repo state."""
    pool = request.app.state.db_pool
    since = date.today() - timedelta(days=_UNKNOWN_DAYS)
    async with pool.acquire() as conn:
        events = await conn.fetch(
            f"SELECT {', '.join(_EVENT_COLUMNS)} FROM finance.journal_index "
            # `coalesce` because a due has no `occurred_on` — ordering on that
            # column alone files every unpaid bill under a NULL. NULLS LAST so
            # a row with neither date does not masquerade as the newest.
            "ORDER BY coalesce(occurred_on, due_on) DESC NULLS LAST, created_at DESC "
            f"LIMIT {_EVENT_LIMIT}"
        )
        unknown_count = await conn.fetchval(
            "SELECT count(*) FROM finance.journal_index "
            "WHERE kind = 'transaction' AND account LIKE '%:unknown' AND occurred_on >= $1",
            since,
        )
        dues_open = await conn.fetchval(
            "SELECT count(*) FROM finance.journal_index "
            f"WHERE kind IN ('due','failed') AND linked_message_id IS NULL AND {OPEN_DUE_SQL}"
        )
    cfg = books.config_from_settings(settings)
    try:
        unpushed = await books.unpushed_commits(cfg)
    except Exception as exc:  # noqa: BLE001 — a missing/degraded checkout is a
        # counter of 0, not a 500: everything else on this page comes from
        # Postgres and is still worth rendering.
        logger.warning("money_unpushed_commits_failed error=%s", str(exc)[:200])
        unpushed = 0
    return {
        "events": [_event(r) for r in events],
        "unknown_count": int(unknown_count or 0),
        "dues_open": int(dues_open or 0),
        "unpushed_commits": unpushed,
        "books_configured": bool(cfg.repo_url) or (cfg.path / ".git").exists(),
        "home_currency": settings.home_currency,
    }


def _latest_close_sync(base: Path) -> dict | None:
    """The newest `reports/monthly/*.md` in the books checkout, read verbatim.

    Newest is the highest filename: `MonthCloseFlow` writes `<YYYY-MM>.md`, so
    lexicographic order IS chronological order and it does not depend on an
    mtime that a fresh `git clone` rewrites to the checkout time.
    """
    try:
        names = sorted(p.name for p in base.iterdir() if p.is_file() and p.suffix == ".md")
    except (FileNotFoundError, NotADirectoryError):
        return None  # no checkout yet, or reports/ has never been written
    except OSError as exc:
        # Everything else — a permissions error, an I/O error, a half-finished
        # clone — renders identically to "no close filed yet". Say so in the
        # log, or the page quietly reports an empty month forever.
        logger.warning("money_digest_list_failed dir=%s error=%s", base, str(exc)[:200])
        return None
    if not names:
        return None
    newest = names[-1]
    try:
        text = (base / newest).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("money_digest_read_failed file=%s error=%s", newest, str(exc)[:200])
        return None
    return {"path": f"{_REPORTS_REL}/{newest}", "markdown": text}


@router.get("/digest")
async def money_digest(settings: Settings = Depends(get_settings)) -> dict:
    """The latest monthly close, read off the books repo, or {digest: None}."""
    base = books.config_from_settings(settings).path / _REPORTS_REL
    return {"digest": await asyncio.to_thread(_latest_close_sync, base)}


@router.post("/{flow}/run")
async def trigger_flow(
    flow: str,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Manually trigger a money flow by name.

    Returns 503 when no Temporal client is connected, 409 when the
    feature flag is off, 400 for unknown flow names. Body is forwarded
    as the workflow config dict.
    """
    client = require_temporal_client(request)
    if not getattr(settings, "money_hygiene_enabled", False):
        raise HTTPException(
            status_code=409,
            detail="money_hygiene disabled — set AEGIS_MONEY_HYGIENE_ENABLED=true",
        )
    try:
        body = await request.json()
    except Exception:
        body = {}
    handle = await _start_workflow(flow, body or {}, client)
    return {"ok": True, "workflow_id": handle.id}


# --------------------------------------------------------------- the accounting

# How far back the bills page looks for a bill that has since been paid. Long
# enough to cover a monthly cycle and the few days a receipt takes to arrive.
_PAID_DAYS = 45

# The unexplained queue is capped so one bad import cannot render ten thousand
# rows into a browser. The COUNTS below it are a separate aggregate over the
# whole window, never a sum of what fits on screen (issue #391).
_UNKNOWN_LIMIT = 200


def _home_symbol(settings: Settings) -> str:
    """The commodity every report on this page is converted to.

    `currency_symbol` is the same map the formatter uses, so the symbol the
    reader sees and the symbol hledger was asked to convert into cannot drift.
    A currency with no symbol falls back to the rupee, which is what the rest
    of this lane hardcodes.
    """
    return currency_symbol(getattr(settings, "home_currency", "INR")) or "₹"


def _balance_report(text: str) -> dict:
    """One `hledger bal -O csv` report as rows plus hledger's own total.

    **Amount cells travel verbatim.** The journal is the record and hledger has
    already rendered its answer; re-parsing the digits here would make this
    page a second opinion about a figure that was not in doubt. It also keeps
    the one case that matters honest: with no rate in `prices.journal` hledger
    reports a mixed cell — `"$-40.00, ₹98,765.50"` — and every scheme for
    turning that into a single rupee number is wrong in a way the reader cannot
    see. Shown as hledger wrote it, they can.

    The grand total is the row whose account cell ends in ":" (`"Total:"`).
    That is the same rule the weekly brief uses to tell a report label from an
    account: an account name never ends in a colon, because that would be an
    empty final component, while a bare `":" in cell` test lets `"Total:"`
    through and counts the total as an account.
    """
    rows: list[dict[str, str]] = []
    total: str | None = None
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        account, cell = row[0].strip(), row[1].strip()
        if account.lower() == "account":
            continue  # the csv header
        if account.endswith(":"):
            total = cell
            continue
        if account:
            rows.append({"account": account, "balance": cell})
    return {"rows": rows, "total": total}


@router.get("/balances")
async def money_balances(
    request: Request, settings: Settings = Depends(get_settings)
) -> dict:
    """Where the money stands, and what changed this month.

    Two hledger reports, both converted to the home commodity:

    * everything the owner has and owes right now, leaf accounts, biggest
      first;
    * income and expenses since the 1st, two levels deep, which is the
      "what changed" half.

    `books_ok` is false and `error` names the reason when the checkout is
    missing or hledger refuses — the page then still renders its other
    sections rather than a blank screen.
    """
    cfg = books.config_from_settings(settings)
    symbol = _home_symbol(settings)
    today = date.today()
    month_start = today.replace(day=1)
    # `-e` is exclusive, so the window ends the day AFTER today or a
    # transaction dated today is silently left out of "this month".
    month_end = (today + timedelta(days=1)).isoformat()
    out: dict[str, Any] = {
        "as_of": today.isoformat(),
        "month_start": month_start.isoformat(),
        "home_currency": getattr(settings, "home_currency", "INR"),
        "home_symbol": symbol,
        "books_ok": True,
        "error": None,
        "standing": {"rows": [], "total": None},
        "month": {"rows": [], "total": None},
    }
    try:
        standing = await books.run_hledger(
            ["bal", "-X", symbol, "assets", "liabilities", "--flat", "--sort-amount"],
            cfg,
            output_format="csv",
        )
        month = await books.run_hledger(
            [
                "bal", "-X", symbol, "-b", month_start.isoformat(), "-e", month_end,
                "income", "expenses", "--depth", "2", "--sort-amount",
            ],
            cfg,
            output_format="csv",
        )
    except books.BooksError as exc:
        # Not a 500: the books are a git checkout that can be absent,
        # mid-clone or unreadable, and none of that is a reason for the page
        # to lose its bills, statements and desk as well.
        logger.warning("money_balances_unavailable error=%s", str(exc)[:200])
        return out | {"books_ok": False, "error": str(exc)[:300]}
    return out | {"standing": _balance_report(standing), "month": _balance_report(month)}


def _due(row) -> dict:
    """One bill, JSON-safe. The amount stays the ledger's own string."""
    return {
        "message_id": row["message_id"],
        "payee": row["payee"],
        "amount": None if row["amount"] is None else str(row["amount"]),
        "currency": row["currency"],
        "due_on": row["due_on"].isoformat() if row["due_on"] else None,
        "kind": row["kind"],
        "entity": row["entity"],
        "todoist_ref": row["todoist_ref"],
    }


@router.get("/dues")
async def money_dues(request: Request) -> dict:
    """The bills: what is still owed, what is late, and what was just paid.

    "Open" is `OPEN_DUE_SQL`, the same predicate the money counter, the month
    close and `/state` use — a ₹0 invoice, an undated one, a ticked-off one and
    a stale untasked one are all not things you still owe. Using it here rather
    than a fresh WHERE clause is the point: four screens quoting four different
    "still open" numbers is the failure this endpoint exists to avoid.

    Overdue is a date comparison over that same list, never a second query, so
    the two counts can never add up to more than the list itself.
    """
    pool = request.app.state.db_pool
    today = date.today()
    async with pool.acquire() as conn:
        open_rows = await conn.fetch(
            "SELECT message_id, payee, amount, currency, due_on, kind, entity, todoist_ref "
            "FROM finance.journal_index "
            "WHERE kind IN ('due','failed') AND linked_message_id IS NULL "
            f"  AND {OPEN_DUE_SQL} "
            "ORDER BY due_on"
        )
        paid_rows = await conn.fetch(
            "SELECT message_id, payee, amount, currency, due_on, kind, entity, todoist_ref, "
            "       linked_message_id, updated_at "
            "FROM finance.journal_index "
            "WHERE kind IN ('due','failed') AND linked_message_id IS NOT NULL "
            "  AND updated_at >= $1 ORDER BY updated_at DESC LIMIT 50",
            datetime.now(UTC) - timedelta(days=_PAID_DAYS),
        )
        # A bill whose task the user ticked off. It is not in `open` above and
        # it was never matched to a payment, so without this it vanishes from
        # the page entirely — and "I ticked it, did it actually settle?" is a
        # question the owner is entitled to ask.
        ticked = await conn.fetchval(
            "SELECT count(*) FROM finance.journal_index "
            "WHERE kind IN ('due','failed') AND linked_message_id IS NULL "
            f"  AND due_on IS NOT NULL AND {TICKED_OFF_SQL}"
        )
    dues = [_due(r) for r in open_rows]
    paid = [
        _due(r) | {
            "linked_message_id": r["linked_message_id"],
            "paid_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in paid_rows
    ]
    overdue = [d for d in dues if d["due_on"] and d["due_on"] < today.isoformat()]
    return {
        "as_of": today.isoformat(),
        "open": dues,
        "overdue_count": len(overdue),
        "paid_recently": paid,
        "paid_days": _PAID_DAYS,
        "ticked_off_count": int(ticked or 0),
    }


@router.get("/unknowns")
async def money_unknowns(
    request: Request, days: int = Query(_UNKNOWN_DAYS, ge=1, le=3650)
) -> dict:
    """The clean-up queue: postings filed to an `:unknown` account.

    These are transactions the books recorded but could not classify, so the
    money is in the journal and in no meaningful account. `totals` is its own
    aggregate over the whole window, NOT a sum of `rows` — the list is capped,
    and a total of the top 200 presented as the total is the one number on this
    page whose whole job is to be the real one (issue #391).

    Grouped by currency as well as account: adding a dollar to a rupee gives a
    figure that is true of nothing.
    """
    pool = request.app.state.db_pool
    since = date.today() - timedelta(days=days)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT message_id, payee, amount, currency, occurred_on, channel, account, "
            "       entity, journal_file, instrument "
            "FROM finance.journal_index "
            "WHERE kind = 'transaction' AND account LIKE '%:unknown' AND occurred_on >= $1 "
            # `amount IS NOT NULL` because a transaction the writer REFUSED is
            # indexed with no amount at all, and it sorts and formats as
            # nothing useful. It is still counted in `totals` below.
            "  AND amount IS NOT NULL "
            f"ORDER BY abs(amount) DESC LIMIT {_UNKNOWN_LIMIT}",
            since,
        )
        totals = await conn.fetch(
            "SELECT account, currency, count(*) AS n, "
            # `abs()` on both sides: the index stores a magnitude with the sign
            # carried by `direction`, so a stray negative would cancel a real
            # one instead of adding to it.
            "       coalesce(sum(abs(amount)), 0) AS total "
            "FROM finance.journal_index "
            "WHERE kind = 'transaction' AND account LIKE '%:unknown' AND occurred_on >= $1 "
            "GROUP BY account, currency ORDER BY account, currency",
            since,
        )
    return {
        "days": days,
        "since": since.isoformat(),
        "limit": _UNKNOWN_LIMIT,
        "rows": [
            {
                "message_id": r["message_id"],
                "payee": r["payee"],
                "amount": str(r["amount"]),
                "currency": r["currency"],
                "occurred_on": r["occurred_on"].isoformat() if r["occurred_on"] else None,
                "channel": r["channel"],
                "account": r["account"],
                "entity": r["entity"],
                "instrument": r["instrument"],
                "journal_file": r["journal_file"],
            }
            for r in rows
        ],
        "totals": [
            {
                "account": r["account"],
                "currency": r["currency"],
                "count": int(r["n"]),
                "total": str(r["total"]),
            }
            for r in totals
        ],
    }


def _months_between(start: date, end: date) -> list[date]:
    """The first of every month from `start`'s month through `end`'s, inclusive."""
    out: list[date] = []
    cur = start.replace(day=1)
    while cur <= end:
        out.append(cur)
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


@router.get("/statements")
async def money_statements(request: Request) -> dict:
    """Bank statements: which periods are in, which reconciled, what is unmatched.

    A month counts as covered when some statement's period overlaps it at all,
    because a card cycle runs 20th to 19th and would otherwise mark both months
    it touches as missing. Only months up to the last COMPLETE one are checked:
    this month's statement has not been issued yet, so calling it missing would
    put a permanent false alarm on the page.
    """
    pool = request.app.state.db_pool
    today = date.today()
    # The last complete month: the day before the 1st of this one.
    last_complete = today.replace(day=1) - timedelta(days=1)
    async with pool.acquire() as conn:
        statements = await conn.fetch(
            "SELECT statement_id, instrument, period_start, period_end, opening_balance, "
            "       closing_balance, rows, reconciled_at "
            "FROM finance.statements ORDER BY instrument, period_end DESC"
        )
        counts = await conn.fetch(
            "SELECT statement_id, "
            "  count(*) FILTER (WHERE matched_msgid IS NOT NULL) AS matched, "
            "  count(*) FILTER (WHERE matched_msgid IS NULL AND skip_reason IS NULL) AS unmatched, "
            "  count(*) FILTER (WHERE skip_reason IS NOT NULL) AS skipped "
            "FROM finance.statement_rows GROUP BY statement_id"
        )
        marks = await conn.fetch(
            "SELECT instrument, through_date, statement_id, updated_at "
            "FROM finance.reconciled_through ORDER BY instrument"
        )
    by_id = {r["statement_id"]: r for r in counts}
    watermark = {r["instrument"]: r for r in marks}

    accounts: dict[str, dict] = {}
    for s in statements:
        acc = accounts.setdefault(
            s["instrument"],
            {"instrument": s["instrument"], "statements": [], "covered": set()},
        )
        c = by_id.get(s["statement_id"])
        acc["statements"].append({
            "statement_id": s["statement_id"],
            "period_start": s["period_start"].isoformat(),
            "period_end": s["period_end"].isoformat(),
            # The bank's own printed figures. Strings, like every other amount
            # this module reports.
            "opening_balance": None if s["opening_balance"] is None else str(s["opening_balance"]),
            "closing_balance": None if s["closing_balance"] is None else str(s["closing_balance"]),
            "rows": int(s["rows"] or 0),
            "reconciled_at": s["reconciled_at"].isoformat() if s["reconciled_at"] else None,
            "matched": int(c["matched"]) if c else 0,
            "unmatched": int(c["unmatched"]) if c else 0,
            "skipped": int(c["skipped"]) if c else 0,
        })
        acc["covered"].update(
            m for m in _months_between(s["period_start"], s["period_end"])
        )

    out = []
    for instrument in sorted(accounts):
        acc = accounts[instrument]
        first = min(date.fromisoformat(s["period_start"]) for s in acc["statements"])
        missing = [
            m.isoformat()
            for m in _months_between(first, last_complete)
            if m not in acc["covered"]
        ]
        mark = watermark.get(instrument)
        out.append({
            "instrument": instrument,
            "statements": acc["statements"],
            "missing_months": missing,
            "reconciled_through": mark["through_date"].isoformat() if mark else None,
            "unmatched": sum(s["unmatched"] for s in acc["statements"]),
            "rows": sum(s["rows"] for s in acc["statements"]),
        })
    # An account with a watermark but no statement row left (a purged import,
    # say) would otherwise disappear from a page about coverage.
    for instrument in sorted(set(watermark) - set(accounts)):
        out.append({
            "instrument": instrument,
            "statements": [],
            "missing_months": [],
            "reconciled_through": watermark[instrument]["through_date"].isoformat(),
            "unmatched": 0,
            "rows": 0,
        })
    return {"as_of": today.isoformat(), "through_month": last_complete.isoformat(), "accounts": out}


# ------------------------------------------------------------- the trading desk

# How many past decision dates the desk's history shows by default. One trading
# month, so the page opens on something a reader can hold in their head.
_DESK_HISTORY_DAYS = 30


def _round(value: float | None, places: int = 2) -> float | None:
    """A desk figure, rounded before it crosses the wire.

    The desk is deliberately float arithmetic — `desk_math`'s own docstring
    says so, because this is a score and not the ledger — so the rounding
    authority is here, on the server, exactly as it is for the ledger's
    Decimals. The browser formats what it is given and never rounds again.
    """
    return None if value is None else round(value, places)


async def _desk_problems(conn) -> list[dict]:
    """Everything the desk is currently complaining about.

    Keyed on `subject_kind`, which is the desk's own identity in the hub
    (`trading_desk.SUBJECT_KIND`) — never on a title or a class list, because
    the hub owns problem identity and a page that matches on text would miss a
    finding the day someone rewords it.
    """
    rows = await conn.fetch(
        "SELECT id::text AS id, class, subject, title, severity, status, occurrences, "
        "       first_seen_at, last_seen_at, muted_until, todoist_task_id, metadata "
        "FROM problems WHERE subject_kind = $1 AND closed_at IS NULL "
        "ORDER BY last_seen_at DESC",
        trading_desk.SUBJECT_KIND,
    )
    return [
        {
            "id": r["id"],
            "class": r["class"],
            "subject": r["subject"],
            "title": r["title"],
            "severity": r["severity"],
            "status": r["status"],
            "occurrences": int(r["occurrences"]),
            "first_seen_at": r["first_seen_at"].isoformat(),
            "last_seen_at": r["last_seen_at"].isoformat(),
            "muted_until": r["muted_until"].isoformat() if r["muted_until"] else None,
            "todoist_task_id": r["todoist_task_id"],
            # The finding's own sentence, written by the desk when it raised
            # the problem. It says what to do about it, so it is what the page
            # shows rather than a class name the reader has to decode.
            "description": (r["metadata"] or {}).get("description"),
        }
        for r in rows
    ]


@router.get("/desk")
async def desk_state(request: Request) -> dict:
    """The trading desk today: what it holds, what it is worth, how it is doing.

    The positions come from `desk_math.replay` over the filled orders — the
    same replay the daily run uses — and never from counting buys minus sells:
    a split leaves more shares than were ever bought, so a count calls a live
    position closed and loses it.

    The score is `trading_desk.month_summary`, the identical call the monthly
    close makes. It returns None before the first fill, and that is reported as
    `score: null` rather than a zeroed-out scorecard that looks like a result.

    `_fills` and `_bars` are the desk's own module-private readers, called here
    rather than copied. Copying them is the tempting alternative and the wrong
    one: `_bars` carries a rule no reimplementation would rediscover — two desk
    names can share one Yahoo symbol (the desk holds SHARIABEES and benchmarks
    against SHARIABEES.NS), so each Yahoo symbol's bars go to every name that
    asked for it. A second copy of that read is a second set of prices that can
    drift from the ones the desk actually traded on.
    """
    pool = request.app.state.db_pool
    rules = await trading_desk.load_rules(pool)
    today = datetime.now(trading_desk.MARKET_TZ).date()

    fills = await trading_desk._fills(pool)
    async with pool.acquire() as conn:
        pending = await conn.fetch(
            "SELECT id::text AS id, data_date, created_day, seq, symbol, asset_class, side, qty, "
            "       ref_price FROM finance.desk_orders "
            "WHERE status = 'pending' ORDER BY created_day, seq"
        )
        latest_plan = await conn.fetchrow(
            "SELECT data_date, outcome, findings, skipped, planned_at "
            "FROM finance.desk_plans ORDER BY data_date DESC LIMIT 1"
        )
        problems = await _desk_problems(conn)

    ever = {f.symbol: f.asset_class for f in fills}
    symbols = (
        set(ever)
        | {r["symbol"] for r in pending}
        | {rules.benchmark, rules.context_benchmark, trading_desk.INDEX}
    )
    bars = await trading_desk._bars(pool, symbols)
    book = desk_math.replay(fills, bars, rules.capital, today)
    total = desk_math.value(book, bars, today)

    positions = []
    for symbol, qty in book.held().items():
        series = bars.get(symbol, [])
        close = desk_math.close_on(series, today)
        avg = book.avg_cost(symbol)
        # The same expression `month_summary` values a holding with: last close
        # if there is one, otherwise cost. A holding with no price at all is
        # flagged rather than quietly carried at cost.
        worth = qty * (close if close is not None else avg)
        priced_on = max((b.day for b in series if b.close is not None), default=None)
        positions.append({
            "symbol": symbol,
            "asset_class": ever.get(symbol, ""),
            "qty": qty,
            "avg_cost": _round(avg, 4),
            "cost": _round(qty * avg),
            "last_close": _round(close, 4),
            "priced_on": priced_on.isoformat() if priced_on else None,
            "priced": close is not None,
            "value": _round(worth),
            "gain": _round(worth - qty * avg),
            "weight": _round(worth / total, 4) if total else None,
        })
    positions.sort(key=lambda p: -(p["value"] or 0))

    month_first = today.replace(day=1)
    next_first = (month_first + timedelta(days=32)).replace(day=1)
    score = await trading_desk.month_summary(pool, month_first, next_first)

    return {
        "as_of": today.isoformat(),
        "mode": rules.mode,
        "capital": rules.capital,
        "benchmark": rules.benchmark,
        "context_benchmark": rules.context_benchmark,
        "value": _round(total),
        "cash": _round(book.cash),
        "cash_pct": _round(book.cash / total, 4) if total else None,
        "invested": _round(total - book.cash),
        "gain": _round(total - rules.capital),
        "realised": _round(sum(r.gain for r in book.realised)),
        "tax_if_sold_today": _round(desk_math.tax_owed(book.realised, rules)),
        "positions": positions,
        "pending": [
            {
                "id": r["id"],
                "data_date": r["data_date"].isoformat(),
                "created_day": r["created_day"].isoformat(),
                "symbol": r["symbol"],
                "asset_class": r["asset_class"],
                "side": r["side"],
                "qty": int(r["qty"]),
                "ref_price": _round(float(r["ref_price"]), 4),
                "est_value": _round(int(r["qty"]) * float(r["ref_price"])),
            }
            for r in pending
        ],
        "latest_plan": None if latest_plan is None else {
            "data_date": latest_plan["data_date"].isoformat(),
            "outcome": latest_plan["outcome"],
            "findings": list(latest_plan["findings"] or []),
            "skipped": list(latest_plan["skipped"] or []),
            "planned_at": latest_plan["planned_at"].isoformat(),
        },
        "score": score,
        "problems": problems,
    }


@router.get("/desk/history")
async def desk_history(
    request: Request, limit: int = Query(_DESK_HISTORY_DAYS, ge=1, le=365)
) -> dict:
    """Each decision date the desk acted on, with the orders it wrote.

    A plan row exists for every date the desk reached, whatever it decided, so
    this is also the record of the days it held back: `outcome` says which, and
    the plan's own stored findings say why. The findings are read off the plan
    rather than off the problems table on purpose — a rerun after the fact
    cannot resolve a problem that was true on the day, so the day's own copy is
    the honest one.
    """
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        plans = await conn.fetch(
            "SELECT data_date, outcome, findings, skipped, planned_at "
            "FROM finance.desk_plans ORDER BY data_date DESC LIMIT $1",
            limit,
        )
        days = [p["data_date"] for p in plans]
        orders = await conn.fetch(
            "SELECT data_date, seq, created_day, symbol, asset_class, side, qty, ref_price, "
            "       status, fill_date, fill_price, costs, price_source, reason "
            "FROM finance.desk_orders WHERE data_date = ANY($1::date[]) "
            "ORDER BY data_date DESC, seq",
            days,
        )
    by_day: dict[date, list[dict]] = {}
    for o in orders:
        by_day.setdefault(o["data_date"], []).append({
            "seq": int(o["seq"]),
            "created_day": o["created_day"].isoformat(),
            "symbol": o["symbol"],
            "asset_class": o["asset_class"],
            "side": o["side"],
            "qty": int(o["qty"]),
            "ref_price": _round(float(o["ref_price"]), 4),
            "status": o["status"],
            "fill_date": o["fill_date"].isoformat() if o["fill_date"] else None,
            "fill_price": _round(float(o["fill_price"]), 4) if o["fill_price"] is not None else None,
            "costs": _round(float(o["costs"])) if o["costs"] is not None else None,
            "price_source": o["price_source"],
            "reason": o["reason"],
        })
    return {
        "limit": limit,
        "days": [
            {
                "data_date": p["data_date"].isoformat(),
                "outcome": p["outcome"],
                "findings": list(p["findings"] or []),
                "skipped": list(p["skipped"] or []),
                "planned_at": p["planned_at"].isoformat(),
                "orders": by_day.get(p["data_date"], []),
            }
            for p in plans
        ],
    }
