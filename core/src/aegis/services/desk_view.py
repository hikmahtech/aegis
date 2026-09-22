"""What the trading desk holds and has done, as one read both surfaces share.

The admin Trading desk page (`GET /api/admin/money/desk` and `/desk/history`)
and the `desk_status` chat tool both call these two functions. They used to
live in the route, which left the desk's own agent with no way to see its book:
asked for its holdings, Maou could read only the ledger and said "no securities
holdings recorded" while the desk held a portfolio. One read means the page and
the chat can never disagree about what the desk holds.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import asyncpg

from aegis.services import desk_math, trading_desk

HISTORY_DAYS = 30


def _round(value: float | None, places: int = 2) -> float | None:
    """A desk figure, rounded before it crosses the wire.

    The desk is deliberately float arithmetic — `desk_math`'s own docstring
    says so, because this is a score and not the ledger — so the rounding
    authority is here, on the server, exactly as it is for the ledger's
    Decimals. The browser formats what it is given and never rounds again.
    """
    return None if value is None else round(value, places)


async def desk_problems(conn) -> list[dict]:
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


async def snapshot(pool: asyncpg.Pool) -> dict:
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
    rules = await trading_desk.load_rules(pool)
    today = datetime.now(rules.tz()).date()

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
        problems = await desk_problems(conn)

    ever = {f.symbol: f.asset_class for f in fills}
    # An unconfigured desk names no calendar and no benchmarks, and an empty
    # string is not a symbol anything can be looked up by.
    symbols = set(ever) | {r["symbol"] for r in pending} | {
        s for s in (rules.benchmark, rules.context_benchmark, rules.calendar_symbol) if s
    }
    bars = await trading_desk._bars(pool, rules, symbols)
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
    score = await trading_desk.month_summary(pool, month_first, next_first, today=today)

    return {
        "as_of": today.isoformat(),
        "mode": rules.mode,
        # False when no trading calendar is set: the desk then runs nothing, so
        # the page says that rather than showing an idle desk that looks live.
        "configured": rules.configured(),
        "capital": rules.capital,
        "currency": rules.currency,
        "benchmark": rules.benchmark,
        "context_benchmark": rules.context_benchmark,
        # True when a tax rate has been stated. Without one the desk deducts
        # nothing, and a zero must not read as "you owe no tax".
        "taxed": bool(rules.tax_rate or rules.long_term_rate),
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


async def history(pool: asyncpg.Pool, limit: int = HISTORY_DAYS) -> dict:
    """Each decision date the desk acted on, with the orders it wrote.

    A plan row exists for every date the desk reached, whatever it decided, so
    this is also the record of the days it held back: `outcome` says which, and
    the plan's own stored findings say why. The findings are read off the plan
    rather than off the problems table on purpose — a rerun after the fact
    cannot resolve a problem that was true on the day, so the day's own copy is
    the honest one.
    """
    async with pool.acquire() as conn:
        plans = await conn.fetch(
            "SELECT data_date, outcome, findings, skipped, planned_at "
            "FROM finance.desk_plans ORDER BY data_date DESC LIMIT $1",
            limit,
        )
        days = [p["data_date"] for p in plans]
        orders = await conn.fetch(
            "SELECT data_date, seq, created_day, symbol, asset_class, side, qty, ref_price, "
            "       status, fill_date, fill_price, costs, price_source, price_kind, reason "
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
            "price_kind": o["price_kind"],
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
