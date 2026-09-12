"""Maou's trading desk, paper mode: the daily run and its storage.

Spec: docs/superpowers/specs/2026-09-12-maou-trading-desk-design.md. The
arithmetic is in :mod:`desk_math`; this module reads and writes
``finance.desk_*`` and reports problems through the hub. Nothing here asks a
model anything.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg
import structlog

from aegis.connectors.ansaar import AnsaarError
from aegis.services import desk_math as dm
from aegis.services import hub_watch

logger = structlog.get_logger()

MARKET_TZ = ZoneInfo("Asia/Kolkata")
DESK_SLUG = "trading-desk-daily"
INDEX = "^NSEI"  # the trading calendar: its bars are the market days
SOURCE = "money"
SUBJECT_KIND = "trading_desk"
DAILY_CLASSES = ["desk_decisions_stale", "desk_decisions_suspect", "desk_source_error", "desk_price_missing"]
MONTHLY_CLASSES = ["desk_below_expectation"]
FETCH_BACK_DAYS = 30
REFETCH_OVERLAP_DAYS = 5
PRICE_GRACE_DAYS = 3  # market days before an unfillable order is cancelled
PRICE_STALE_DAYS = 7  # calendar days before a price is too old to size or sell on
STALE_INDEX_DAYS = 6  # calendar days before the market calendar itself looks wrong;
# a long weekend plus a holiday is 4 to 5 days, so 6 keeps the alarm honest
ANSAAR_FALLBACK_DAYS = 7  # how far back the fallback may reach; see _refresh


def yahoo_symbol(symbol: str) -> str:
    """NSE symbol to Yahoo's (``TCS`` to ``TCS.NS``). An index, or a symbol that
    already names its exchange, is left alone."""
    return symbol if symbol.startswith("^") or "." in symbol else f"{symbol}.NS"


def _finding(klass: str, subject: str, title: str, description: str) -> dict:
    return {
        "klass": klass,
        "subject": subject,
        "title": title,
        "severity": "warning",
        "payload": {"description": description},
    }


def _halt(meta: Any) -> dict | None:
    """What ansaar said about a risk halt on the day it served, or None.

    Only an explicit ``halted: true`` counts. A missing field, an older
    ansaar, or a call that failed all mean the desk knows nothing, and the desk
    never reads a halt into silence (spec §5)."""
    if not isinstance(meta, dict) or meta.get("halted") is not True:
        return None
    detail = meta.get("halt")
    return detail if isinstance(detail, dict) else {}


def _f(raw: Any) -> float | None:
    return float(raw) if raw is not None else None


def _n(raw: float | None) -> float | None:
    """Round before a numeric column, so the stored figure reads like a price
    and not a float's binary expansion."""
    return round(raw, 4) if raw is not None else None


def _ts(raw: Any) -> datetime | None:
    """ClickHouse's ``updated_at`` as served (``2026-09-11 17:30:11.358``), read as UTC."""
    try:
        parsed = datetime.fromisoformat(str(raw).replace(" ", "T"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def load_rules(pool: asyncpg.Pool) -> dm.Rules:
    """The desk's rules from its activities row, read on every run so an edit
    needs no redeploy (spec §12). No row means the defaults."""
    cfg = await pool.fetchval("SELECT config FROM activities WHERE slug = $1", DESK_SLUG)
    return dm.Rules.from_config(cfg if isinstance(cfg, dict) else None)


# --- prices ------------------------------------------------------------------


async def _store_bars(pool: asyncpg.Pool, symbol: str, bars: list[dict], source: str, today: date) -> None:
    """Store bars under ``symbol`` (Yahoo form). The first close seen for a day
    wins, because Yahoo rewrites past closes after a split, and no bar for today
    or later is kept, because it could be an intraday price (spec §3 step 3)."""
    rows = [
        (symbol, b["day"], _n(b.get("close")), _n(b.get("split_ratio")), _n(b.get("dividend")), source)
        for b in bars
        if b["day"] < today
    ]
    if not rows:
        return
    await pool.executemany(
        "INSERT INTO finance.desk_prices (symbol, date, close, split_ratio, dividend, source) "
        "VALUES ($1, $2, $3, $4, $5, $6) "
        "ON CONFLICT (symbol, date) DO UPDATE SET "
        "  source = CASE WHEN finance.desk_prices.close IS NULL AND EXCLUDED.close IS NOT NULL "
        "           THEN EXCLUDED.source ELSE finance.desk_prices.source END, "
        "  close = COALESCE(finance.desk_prices.close, EXCLUDED.close), "
        "  split_ratio = COALESCE(finance.desk_prices.split_ratio, EXCLUDED.split_ratio), "
        "  dividend = COALESCE(finance.desk_prices.dividend, EXCLUDED.dividend)",
        rows,
    )


async def _unpriced(pool: asyncpg.Pool, ysym: str, days: list[date]) -> set[date]:
    """Which of ``days`` still have no close stored for ``ysym``. A row whose
    close is NULL counts as unpriced: Yahoo answers for a market day with a bar
    that carries no close, and a NULL is no use for filling or sizing."""
    if not days:
        return set()
    rows = await pool.fetch(
        "SELECT date FROM finance.desk_prices "
        "WHERE symbol = $1 AND close IS NOT NULL AND date = ANY($2::date[])",
        ysym, days,
    )
    return set(days) - {r["date"] for r in rows}


async def _refresh(
    pool: asyncpg.Pool,
    finance: Any,
    ansaar: Any,
    symbol: str,
    asset_class: str | None,
    today: date,
    *,
    market_days: list[date] | None = None,
    required: bool = False,
    source_symbol: str | None = None,
) -> None:
    """Fetch and store ``symbol``'s recent bars: Yahoo first, then ansaar for
    each market day Yahoo could not price. Raises only when ``required`` and
    Yahoo fails.

    ``source_symbol`` is the name ansaar knows the instrument by, when that is
    not ``symbol``: a benchmark is named in Yahoo's form (``SHARIABEES.NS``) and
    ansaar wants the NSE symbol (``SHARIABEES``). Bars are always stored under
    ``symbol``'s Yahoo form, whichever source they came from."""
    ysym = yahoo_symbol(symbol)
    latest = await pool.fetchval("SELECT max(date) FROM finance.desk_prices WHERE symbol = $1", ysym)
    start = latest - timedelta(days=REFETCH_OVERLAP_DAYS) if latest else today - timedelta(days=FETCH_BACK_DAYS)
    end = today - timedelta(days=1)
    if start > end:
        return
    try:
        bars = await finance.daily_bars(ysym, start, end)
    except Exception as exc:  # noqa: BLE001 — one symbol's outage must not sink the run
        if required:
            raise
        logger.warning("trading_desk_yahoo_failed", symbol=ysym, error=str(exc)[:200])
        bars = []
    if bars:
        await _store_bars(pool, ysym, bars, "yahoo", today)
    if ansaar is None or asset_class is None:
        return
    # The fallback is per day, not per window (spec §6). Yahoo often answers
    # with a bar whose close is None, so "Yahoo returned something" does not
    # mean every day is priced, and a day left NULL would size and fill on a
    # stale close. ansaar's prices are back-adjusted for splits and dividends
    # (its API.md says so), so only the last few days are safe to keep as
    # traded prices. A symbol Yahoo does not carry at all therefore starts with
    # a short history, which is enough to fill and value it from here on.
    window = max(start, today - timedelta(days=ANSAAR_FALLBACK_DAYS))
    missing = await _unpriced(pool, ysym, [d for d in (market_days or []) if window <= d <= end])
    if not missing:
        return
    try:
        bars = await ansaar.prices(source_symbol or symbol, asset_class, min(missing), max(missing))
    except AnsaarError as exc:
        logger.warning(
            "trading_desk_ansaar_prices_failed", symbol=source_symbol or symbol, error=str(exc)[:200]
        )
        return
    # Only the missing days are filled. ansaar is the second source, so it never
    # adds a day the market calendar has no bar for and the desk never asked about.
    await _store_bars(pool, ysym, [b for b in bars if b["day"] in missing], "ansaar", today)


async def _bars(pool: asyncpg.Pool, symbols: set[str]) -> dict[str, list[dm.Bar]]:
    """Stored bars keyed by the desk's symbol: NSE form for instruments, and the
    benchmarks as named. Each list is sorted by day.

    Two desk names can share one Yahoo symbol: the desk holds SHARIABEES and
    names its benchmark SHARIABEES.NS, and both are SHARIABEES.NS to Yahoo. So
    each Yahoo symbol carries every name that asked for it, and its bars go to
    all of them."""
    by_yahoo: dict[str, list[str]] = defaultdict(list)
    for s in symbols:
        by_yahoo[yahoo_symbol(s)].append(s)
    rows = await pool.fetch(
        "SELECT symbol, date, close, split_ratio, dividend, source FROM finance.desk_prices "
        "WHERE symbol = ANY($1::text[]) ORDER BY symbol, date",
        list(by_yahoo),
    )
    out: dict[str, list[dm.Bar]] = {s: [] for s in symbols}
    for r in rows:
        stored = dm.Bar(r["date"], _f(r["close"]), _f(r["split_ratio"]), _f(r["dividend"]), r["source"])
        for name in by_yahoo[r["symbol"]]:
            out[name].append(stored)
    return out


# --- decisions, orders, plans ------------------------------------------------


async def _store_decisions(pool: asyncpg.Pool, day: date, rows: list[dict]) -> None:
    await pool.executemany(
        "INSERT INTO finance.desk_decisions (data_date, symbol, asset_class, halal_status, direction, "
        "target_weight, confidence, selection_rank, recovery_state, active_kill_conditions, "
        "drawdown_scalar, ml_model_version, source_updated_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) "
        "ON CONFLICT (data_date, symbol) DO NOTHING",
        [
            (
                day,
                str(r["symbol"]),
                str(r.get("asset_class") or ""),
                str(r.get("halal_status") or ""),
                str(r.get("direction") or ""),
                float(r.get("target_weight") or 0.0),
                _f(r.get("confidence")),
                int(r.get("selection_rank") or 0),
                str(r.get("recovery_state") or "NORMAL"),
                str(r.get("active_kill_conditions") or ""),
                _f(r.get("drawdown_scalar")),
                str(r.get("ml_model_version") or ""),
                _ts(r.get("updated_at")),
            )
            for r in rows
        ],
    )


async def _decisions(pool: asyncpg.Pool, day: date) -> list[dm.Decision]:
    rows = await pool.fetch(
        "SELECT symbol, asset_class, halal_status, direction, target_weight, selection_rank, "
        "recovery_state, active_kill_conditions FROM finance.desk_decisions "
        "WHERE data_date = $1 ORDER BY selection_rank, symbol",
        day,
    )
    return [
        dm.Decision(
            r["symbol"], r["asset_class"], r["halal_status"], r["direction"], float(r["target_weight"]),
            r["selection_rank"] or 0, r["recovery_state"] or "NORMAL", r["active_kill_conditions"] or "",
        )
        for r in rows
    ]


async def _fills(pool: asyncpg.Pool) -> list[dm.Fill]:
    rows = await pool.fetch(
        "SELECT symbol, asset_class, side, qty, fill_price, costs, fill_date FROM finance.desk_orders "
        "WHERE status = 'filled' AND mode = 'paper' ORDER BY fill_date, seq"
    )
    return [
        dm.Fill(r["symbol"], r["asset_class"], r["side"], r["qty"], float(r["fill_price"]), float(r["costs"]), r["fill_date"])
        for r in rows
    ]


async def _pending(pool: asyncpg.Pool) -> list[dm.PendingOrder]:
    rows = await pool.fetch(
        "SELECT id::text AS id, symbol, asset_class, side, qty, created_day, data_date, seq "
        "FROM finance.desk_orders WHERE status = 'pending' ORDER BY created_day, seq"
    )
    return [
        dm.PendingOrder(r["id"], r["symbol"], r["asset_class"], r["side"], r["qty"], r["created_day"], r["data_date"], r["seq"])
        for r in rows
    ]


async def _apply_fills(pool: asyncpg.Pool, results: list[dm.FillResult]) -> None:
    for r in results:
        if r.status == "filled":
            await pool.execute(
                "UPDATE finance.desk_orders SET status = 'filled', fill_date = $2, fill_price = $3, "
                "qty = $4, costs = $5, price_source = $6, filled_at = now() "
                "WHERE id = $1::uuid AND status = 'pending'",
                r.order_id, r.fill_day, _n(r.price), r.qty, _n(r.costs), r.source,
            )
        elif r.status == "cancelled":
            await pool.execute(
                "UPDATE finance.desk_orders SET status = 'cancelled', reason = $2 "
                "WHERE id = $1::uuid AND status = 'pending'",
                r.order_id, r.reason,
            )


async def _write_plan(
    pool: asyncpg.Pool,
    day: date,
    outcome: str,
    findings: list[dict],
    skipped: list[str],
    orders: list[dm.Order],
    created_day: date,
    note: str | None = None,
) -> None:
    """The plan row and its orders in one transaction, so a date is acted on once."""
    async with pool.acquire() as conn, conn.transaction():
        inserted = await conn.fetchval(
            "INSERT INTO finance.desk_plans (data_date, mode, outcome, findings, skipped, note) "
            "VALUES ($1, 'paper', $2, $3, $4, $5) ON CONFLICT (data_date) DO NOTHING RETURNING data_date",
            day, outcome, findings, skipped, note,
        )
        if inserted is None:
            return  # another run planned this date first
        await conn.executemany(
            "INSERT INTO finance.desk_orders (mode, data_date, seq, created_day, symbol, asset_class, "
            "side, qty, ref_price) VALUES ('paper', $1, $2, $3, $4, $5, $6, $7, $8)",
            [
                (day, i, created_day, o.symbol, o.asset_class, o.side, o.qty, _n(o.ref_price))
                for i, o in enumerate(orders)
            ],
        )


# --- the run -----------------------------------------------------------------


async def run_tick(
    pool: asyncpg.Pool, *, ansaar: Any, finance: Any, today: date | None = None, project: bool = True
) -> dict:
    """One morning's run (spec §3). Idempotent: a second run on the same day
    changes nothing and raises the same problems."""
    today = today or datetime.now(MARKET_TZ).date()
    out, findings, checked = await _tick(pool, ansaar, finance, today)
    unique = list({(f["klass"], f["subject"]): f for f in findings}.values())
    # Resolve only inside the classes this run checked. A run that stopped early
    # looked at nothing else, so resolving one of those problems would complete
    # its task and raise it again on the next good morning (spec §3).
    await hub_watch.reconcile_findings(
        pool, source=SOURCE, subject_kind=SUBJECT_KIND, classes=checked, findings=unique, project=project
    )
    out["findings"] = sorted(f["klass"] for f in unique)
    return out


async def _tick(pool: asyncpg.Pool, ansaar: Any, finance: Any, today: date) -> tuple[dict, list[dict], list[str]]:
    """The run's outcome, its findings, and the classes it actually checked. A
    run that stops early checked only the thing that stopped it, so it names no
    class and nothing of its own is resolved."""
    rules = await load_rules(pool)
    out: dict[str, Any] = {"today": today.isoformat()}
    if rules.mode != "paper":
        return out | {"skipped": "mode"}, [
            _finding(
                "desk_source_error", "config", f"Trading desk: mode '{rules.mode}' is not built",
                "Only paper mode exists. Set mode back to paper in the trading-desk-daily config. "
                "Nothing was traded.",
            )
        ], []

    # 1. Find the day.
    try:
        await _refresh(pool, finance, None, INDEX, None, today, required=True)
    except Exception as exc:  # noqa: BLE001 — Yahoo's outage is a finding, not a crash
        return out | {"skipped": "yahoo"}, [
            _finding(
                "desk_source_error", "yahoo", "Trading desk: can't reach Yahoo",
                f"Fetching {INDEX} failed ({type(exc).__name__}), so the desk couldn't tell which "
                "day to trade. Nothing was traded.",
            )
        ], []
    index_days = [b.day for b in (await _bars(pool, {INDEX}))[INDEX] if b.close is not None]
    day = dm.last_trading_day(index_days, today)
    if day is None:
        return out | {"skipped": "no_market_day"}, [
            _finding(
                "desk_source_error", "yahoo", "Trading desk: no market days from Yahoo",
                f"Yahoo returned no {INDEX} bars before {today}. Nothing was traded.",
            )
        ], []
    if (today - day).days > STALE_INDEX_DAYS:
        # Yahoo answers 200 with no data when it rate-limits, which raises
        # nothing and would leave the desk quietly idle on an old date.
        return out | {"skipped": "stale_index"}, [
            _finding(
                "desk_source_error", "yahoo", "Trading desk: the market calendar is stale",
                f"The last {INDEX} bar the desk has is {day}, more than {STALE_INDEX_DAYS} days "
                f"before {today}. Yahoo may be refusing data without saying so. Nothing was traded.",
            )
        ], []
    out["day"] = day.isoformat()

    # 2. Copy.
    findings: list[dict] = []
    ansaar_failure: dict | None = None
    halt: dict | None = None
    try:
        rows, meta = await ansaar.decisions(day)
        await _store_decisions(pool, day, [r for r in rows if str(r.get("data_date") or "")[:10] == day.isoformat()])
        halt = _halt(meta)
    except AnsaarError as exc:
        ansaar_failure = _finding(
            "desk_source_error", "ansaar", "Trading desk: can't reach ansaar",
            f"Fetching the decisions for {day} failed ({exc}). The desk held its positions and "
            "traded nothing that day.",
        )
        findings.append(ansaar_failure)
    decisions = await _decisions(pool, day)

    # 3. Prices. What the desk holds comes from a replay, never from counting
    # buys minus sells: a split leaves more shares than were ever bought, so a
    # count would call a still-open position closed and lose it. So load the
    # bars for every symbol ever filled, replay, and refresh from that.
    fills = await _fills(pool)
    pending = await _pending(pool)
    ever = {f.symbol: f.asset_class for f in fills}
    holdings = dm.replay(fills, await _bars(pool, set(ever)), rules.capital, today).held()
    wanted = {s: ever[s] for s in holdings}
    wanted |= {d.symbol: d.asset_class for d in decisions if d.asset_class in rules.asset_classes}
    wanted |= {o.symbol: o.asset_class for o in pending}
    for symbol, asset_class in sorted(wanted.items()):
        await _refresh(pool, finance, ansaar, symbol, asset_class, today, market_days=index_days)
    # The benchmarks get the same per-day fallback the holdings get, so a day
    # Yahoo cannot price does not stay NULL for ever and quietly bend the score.
    # It needs a mapping, because a benchmark is named in Yahoo's form and
    # ansaar wants the NSE symbol and an asset class; an unmapped benchmark gets
    # no fallback. The index is never backfilled: its bars are the market
    # calendar, and the desk takes that from one source only.
    for bench in {rules.benchmark, rules.context_benchmark} - {INDEX}:
        src = rules.benchmark_prices.get(bench)
        await _refresh(
            pool, finance, ansaar if src else None, bench,
            src["asset_class"] if src else None, today,
            market_days=index_days, source_symbol=src["symbol"] if src else None,
        )
    bars = await _bars(pool, set(wanted) | set(ever))

    # 4. Fill.
    if pending:
        book_now = dm.replay(fills, bars, rules.capital, today)
        results = dm.fill_orders(pending, bars, index_days, book_now, rules, grace_days=PRICE_GRACE_DAYS)
        await _apply_fills(pool, results)
        out["filled"] = sum(r.status == "filled" for r in results)
        fills = await _fills(pool)

    # 5. Plan, once per date. A symbol whose order is still pending is left
    # alone, but the rest of the desk carries on: one order nobody can fill used
    # to stop every other name trading until it was cancelled, three market days
    # later. The names held back are named in the plan's `skipped` list, so the
    # row says which part of the day it did not act on, and their targets are
    # sized again tomorrow, because the pipeline rebuilds them daily.
    planned = await pool.fetchval("SELECT 1 FROM finance.desk_plans WHERE data_date = $1", day)
    if not planned:
        book = dm.replay(fills, bars, rules.capital, day)
        check = dm.check_decisions(decisions, book.held_classes(), rules, halted=halt is not None)
        plan_findings: list[dict] = []
        if check.outcome == "held_stale":
            plan_findings.append(
                ansaar_failure
                or _finding(
                    "desk_decisions_stale", "decisions", f"Trading desk: no decisions for {day}",
                    f"ansaar served no trade decisions for {day}, the last trading day, so the desk "
                    "held its positions and traded nothing. Either the pipeline failed or its risk "
                    "manager halted everything: check trade_decisions and kill_switch_events. This "
                    "clears itself on the first day decisions arrive.",
                )
            )
        if check.problems:
            tail = " The desk held its positions." if check.outcome == "held_suspect" else " The rest traded."
            plan_findings.append(
                _finding(
                    "desk_decisions_suspect", "decisions", f"Trading desk: odd decisions for {day}",
                    " ".join(check.problems) + tail,
                )
            )
        orders: list[dm.Order] = []
        skipped: list[str] = []
        if check.outcome in ("ok", "flatten"):
            # A name whose price has gone stale is left out of the sizing, so
            # plan_orders reports it rather than sizing or selling on an old
            # price. Otherwise a delisted holding would have the same sell
            # planned and cancelled over and over.
            closes: dict[str, float] = {}
            for symbol in {d.symbol for d in check.rows} | set(book.held()):
                series = bars.get(symbol, [])
                latest = max((b.day for b in series if b.close is not None), default=None)
                if latest is None or (day - latest).days > PRICE_STALE_DAYS:
                    continue
                px = dm.close_on(series, day)
                if px is not None:
                    closes[symbol] = px
            open_orders = await pool.fetch(
                "SELECT symbol, side, qty, ref_price FROM finance.desk_orders WHERE status = 'pending'"
            )
            # A pending buy's money is spoken for, so today's buys may not spend
            # it. A pending sell's proceeds are not counted at all: the sale has
            # not happened yet, and the desk should not spend what it has not sold.
            reserved = sum(
                float(o["qty"]) * float(o["ref_price"]) * (1 + rules.cost_pct_per_side)
                for o in open_orders
                if o["side"] == "buy"
            )
            # On a flatten ``check.rows`` is empty, so every holding is a name
            # with no target: plan_orders sizes each as the full exit it already
            # knows how to size, and there is nothing left to buy.
            orders, skipped = dm.plan_orders(
                check.rows, book, closes, rules,
                frozen=frozenset(o["symbol"] for o in open_orders), cash_reserved=reserved,
            )
        note = None
        if check.outcome == "flatten":
            outcome = "flattened"
            note = str((halt or {}).get("reason") or "").strip() or (
                "The trading system said it had halted, and gave no reason."
            )
        elif check.outcome == "ok":
            outcome = "orders" if orders else "no_change"
        else:
            outcome = check.outcome
        await _write_plan(pool, day, outcome, plan_findings, skipped, orders, today, note=note)
        out["planned"] = outcome
    findings += list(await pool.fetchval("SELECT findings FROM finance.desk_plans WHERE data_date = $1", day) or [])

    # A holding with no close for the last few market days.
    days = sorted(index_days)
    if len(days) >= PRICE_GRACE_DAYS:
        cutoff = days[-PRICE_GRACE_DAYS]
        for symbol in sorted(dm.replay(fills, bars, rules.capital, today).held()):
            latest = max((b.day for b in bars.get(symbol, []) if b.close is not None), default=None)
            if latest is None or latest < cutoff:
                findings.append(
                    _finding(
                        "desk_price_missing", symbol, f"Trading desk: no price for {symbol}",
                        f"Neither Yahoo nor ansaar has a close for {symbol} since "
                        f"{latest or 'the desk bought it'}. The desk values it at cost until one arrives.",
                    )
                )
    return out, findings, DAILY_CLASSES


# --- the monthly close ---------------------------------------------------------


async def month_summary(pool: asyncpg.Pool, month_first: date, next_first: date) -> dict | None:
    """The monthly close's desk section (spec §9), or None before the first fill.

    Every value is JSON-safe: it travels through Temporal to the renderer."""
    rules = await load_rules(pool)
    month_end = next_first - timedelta(days=1)
    fills = [f for f in await _fills(pool) if f.day <= month_end]
    if not fills:
        return None
    start = fills[0].day
    bars = await _bars(pool, {f.symbol for f in fills} | {INDEX, rules.benchmark, rules.context_benchmark})
    days = [b.day for b in bars[INDEX] if b.close is not None and start <= b.day <= month_end]
    if not days:
        return None
    series = dm.desk_series(fills, bars, rules.capital, days)
    desk = [(d, v) for d, v, _ in series]
    shares = [(d, w) for d, _, w in series]
    bench = dm.benchmark_values(bars[rules.benchmark], rules.capital, rules.cost_pct_per_side, days)
    context = dm.benchmark_values(bars[rules.context_benchmark], rules.capital, 0.0, days)
    book = dm.replay(fills, bars, rules.capital, month_end)
    end_value = desk[-1][1]
    # Two readings of the same weeks. The headline is the gap to the whole
    # benchmark, because "what would I have earned just buying SHARIABEES with
    # this money" is the owner's real question. The alarm is judged on the gap
    # per rupee actually at risk, because the desk holds about the pipeline's
    # own heat and the benchmark holds everything (spec §8).
    st = dm.stats(dm.weekly_excess(desk, bench))
    invested = dm.stats(dm.weekly_excess(desk, bench, shares))
    weights = dm.weekly_shares(desk, bench, shares)
    held = book.held()
    worth = {s: q * (dm.close_on(bars[s], month_end) or book.avg_cost(s)) for s, q in held.items()}
    orders = await pool.fetch(
        "SELECT status, reason, costs, price_source FROM finance.desk_orders "
        "WHERE (status = 'filled' AND fill_date BETWEEN $1 AND $2) "
        "   OR (status = 'cancelled' AND created_day BETWEEN $1 AND $2)",
        month_first, month_end,
    )
    held_back = await pool.fetch(
        "SELECT outcome, count(*) AS n FROM finance.desk_plans "
        "WHERE data_date BETWEEN $1 AND $2 AND outcome IN ('held_stale', 'held_suspect') GROUP BY outcome",
        month_first, month_end,
    )
    halts = await pool.fetch(
        "SELECT data_date, note FROM finance.desk_plans "
        "WHERE data_date BETWEEN $1 AND $2 AND outcome = 'flattened' ORDER BY data_date",
        month_first, month_end,
    )
    cancelled: dict[str, int] = defaultdict(int)
    for r in orders:
        if r["status"] == "cancelled":
            cancelled[r["reason"] or "unknown"] += 1
    return {
        "since": start.isoformat(),
        "weeks": st.n,
        "capital": rules.capital,
        "value": round(end_value, 2),
        "after_tax": round(end_value - dm.tax_owed(book.realised, rules), 2),
        "benchmark": rules.benchmark,
        "benchmark_value": round(bench[-1][1], 2) if bench else None,
        "context": rules.context_benchmark,
        "context_value": round(context[-1][1], 2) if context else None,
        "mean_gap": st.mean,
        "t": st.t,
        "label": dm.label(st),
        "mean_gap_invested": invested.mean,
        "t_invested": invested.t,
        "label_invested": dm.label(invested),
        "invested_pct": sum(weights) / len(weights) if weights else 0.0,
        "below_expectation": dm.below_expectation(invested, rules.expected_excess_pa),
        "expected_excess_pa": rules.expected_excess_pa,
        "holdings": sorted(held, key=lambda s: -worth[s]),
        "cash_pct": book.cash / end_value if end_value else 0.0,
        "filled": sum(r["status"] == "filled" for r in orders),
        "costs": round(sum(float(r["costs"] or 0) for r in orders if r["status"] == "filled"), 2),
        "cancelled": dict(cancelled),
        "held_back": {r["outcome"]: r["n"] for r in held_back},
        "halts": [{"day": r["data_date"].isoformat(), "note": r["note"] or ""} for r in halts],
        "ansaar_prices": sum(r["status"] == "filled" and r["price_source"] == "ansaar" for r in orders),
        "moves": [
            {"symbol": s, "day": d.isoformat(), "move": round(m, 4)}
            for s, d, m in dm.big_moves(bars, set(held), month_first, month_end)
        ],
    }


async def reconcile_expectation(pool: asyncpg.Pool, summary: dict | None, *, project: bool = True) -> None:
    """The monthly warning check (spec §8). Only the close reconciles this class,
    so while it holds it comes back once a month, not every day."""
    findings: list[dict] = []
    if summary and summary.get("below_expectation"):
        findings.append(
            _finding(
                "desk_below_expectation", "desk", "Trading desk: live results are worse than the backtest promised",
                f"Over {summary['weeks']} weeks, per rupee the desk actually had invested "
                f"({summary['invested_pct']:.0%} of the capital on average), its weekly gap to "
                f"{summary['benchmark']} averaged {summary['mean_gap_invested']:+.2%} "
                f"(t = {summary['t_invested']:.1f}). That is more than two standard errors below the "
                f"{summary['expected_excess_pa']:.0%} a year the backtest implies. The cash the desk "
                "holds is taken out of this, so it is the stock picking that is behind, not the "
                "exposure. Look at the trading system before trusting it with money. This comes back "
                "each month while it stays true.",
            )
        )
    await hub_watch.reconcile_findings(
        pool, source=SOURCE, subject_kind=SUBJECT_KIND, classes=MONTHLY_CLASSES, findings=findings, project=project
    )
