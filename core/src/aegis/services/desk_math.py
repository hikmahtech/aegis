"""The trading desk's arithmetic: pure functions, no I/O.

Spec: docs/superpowers/specs/2026-09-12-maou-trading-desk-design.md (§4-§8).
Everything here takes plain values and returns plain values, so every rule the
spec states is one unit test away. `trading_desk.py` does the reading and writing.

Money is float. `ponytail:` this is a score, not the ledger; Decimal matters
where a live fill is posted to the books, which is the live spec's job.
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

DEFAULT_TAX_RATE = {"equity": 0.20, "etf": 0.30}
_EPS = 1e-9


@dataclass(frozen=True)
class Rules:
    """The desk's knobs (spec §12). ``from_config`` merges an activities row's
    ``config`` over these defaults, so a missing key is never an error."""

    mode: str = "paper"
    capital: float = 100_000.0
    asset_classes: tuple[str, ...] = ("equity", "etf")
    cost_pct_per_side: float = 0.002
    sell_charge_inr: float = 16.0
    band_abs: float = 0.02
    band_rel: float = 0.25
    max_order_pct: float = 0.25
    tax_rate: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TAX_RATE))
    ltcg_rate: float = 0.125
    ltcg_exemption_inr: float = 125_000.0
    benchmark: str = "SHARIABEES.NS"
    context_benchmark: str = "^NSEI"
    expected_excess_pa: float = 0.06
    # Where a benchmark's prices can also come from, when Yahoo has no close for
    # a market day: benchmark name (Yahoo's form) to the NSE symbol and asset
    # class ansaar wants, e.g. {"SHARIABEES.NS": {"symbol": "SHARIABEES",
    # "asset_class": "etf"}}. Empty by default and an unmapped benchmark simply
    # gets no fallback, so this repo ships nobody's tickers.
    benchmark_prices: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: dict | None) -> Rules:
        cfg = cfg or {}
        base = cls()

        def num(key: str) -> float:
            raw = cfg.get(key)
            return float(raw) if raw is not None else float(getattr(base, key))

        rates = {k: float(v) for k, v in (cfg.get("tax_rate") or {}).items()}
        # A mapping needs both halves to be usable, so one missing either is
        # dropped rather than half-applied: the benchmark then behaves as an
        # unmapped one, with no fallback.
        benches = {
            str(name): {"symbol": str(src["symbol"]), "asset_class": str(src["asset_class"])}
            for name, src in (cfg.get("benchmark_prices") or {}).items()
            if isinstance(src, dict) and src.get("symbol") and src.get("asset_class")
        }
        return cls(
            mode=str(cfg.get("mode") or base.mode),
            capital=num("capital"),
            asset_classes=tuple(cfg.get("asset_classes") or base.asset_classes),
            cost_pct_per_side=num("cost_pct_per_side"),
            sell_charge_inr=num("sell_charge_inr"),
            band_abs=num("band_abs"),
            band_rel=num("band_rel"),
            max_order_pct=num("max_order_pct"),
            tax_rate={**DEFAULT_TAX_RATE, **rates},
            ltcg_rate=num("ltcg_rate"),
            ltcg_exemption_inr=num("ltcg_exemption_inr"),
            benchmark=str(cfg.get("benchmark") or base.benchmark),
            context_benchmark=str(cfg.get("context_benchmark") or base.context_benchmark),
            expected_excess_pa=num("expected_excess_pa"),
            benchmark_prices=benches,
        )


@dataclass(frozen=True)
class Decision:
    """One row of the trading system's target portfolio, as the desk copied it."""

    symbol: str
    asset_class: str
    halal_status: str
    direction: str
    target_weight: float
    selection_rank: int
    recovery_state: str = "NORMAL"
    active_kill_conditions: str = ""


@dataclass(frozen=True)
class Check:
    """What the checks before trading decided (spec §5).

    ``outcome`` is ``ok``, ``flatten``, ``held_stale`` or ``held_suspect``.
    ``rows`` is what may trade. ``problems`` are plain-English lines for
    ``desk_decisions_suspect``, and can be non-empty on an ``ok`` day: a bad row
    was dropped and the rest traded.
    """

    outcome: str
    rows: tuple[Decision, ...]
    problems: tuple[str, ...]


def last_trading_day(index_days: list[date], today: date) -> date | None:
    """The latest market day strictly before ``today``, from the index's bars."""
    before = [d for d in index_days if d < today]
    return max(before) if before else None


def check_decisions(
    rows: list[Decision], held_classes: set[str], rules: Rules, *, halted: bool = False
) -> Check:
    """Spec §5. An empty day is stale; odd weights or a held class that vanished
    for no stated reason hold the whole day; a non-halal or short row is dropped.

    ``halted`` is ansaar saying the pipeline's risk manager stopped trading that
    day. The pipeline is then flat, so the desk sells its whole book rather than
    staying fully invested against a risk manager that has pulled out. It is
    only ever true when the source states it: an empty day with no reason is
    still ``held_stale``, because a pipeline failure looks exactly the same and
    dumping a portfolio over a glitch is the failure §5 exists to rule out.

    A day that has rows is traded on its rows, halt or no halt: a halt writes no
    rows, so rows plus a halt flag would be a contradiction, and the rows are the
    thing the pipeline actually decided.
    """
    if not rows:
        return Check("flatten", (), ()) if halted else Check("held_stale", (), ())
    problems: list[str] = []
    kept: list[Decision] = []
    for r in rows:
        if r.halal_status != "COMPLIANT" or r.direction != "LONG":
            problems.append(f"{r.symbol} came as {r.halal_status}/{r.direction}; dropped and never bought.")
        else:
            kept.append(r)
    enabled = [r for r in kept if r.asset_class in rules.asset_classes]
    total = sum(r.target_weight for r in enabled)
    bad = [r for r in enabled if r.target_weight <= 0 or r.target_weight > rules.max_order_pct]
    if total > 1.0 + 1e-6:
        problems.append(f"Target weights add up to {total:.1%}, more than the whole portfolio.")
    for r in bad:
        problems.append(
            f"{r.symbol} has a target weight of {r.target_weight:.1%}, outside 0-{rules.max_order_pct:.0%}."
        )
    if total > 1.0 + 1e-6 or bad:
        return Check("held_suspect", (), tuple(problems))
    explained = any(r.active_kill_conditions or r.recovery_state != "NORMAL" for r in rows)
    present = {r.asset_class for r in kept}
    vanished = sorted(c for c in held_classes if c in rules.asset_classes and c not in present)
    if vanished and not explained:
        problems.append(
            f"The desk holds {', '.join(vanished)} but today's decisions have none, "
            "and no kill switch or recovery state explains it."
        )
        return Check("held_suspect", (), tuple(problems))
    return Check("ok", tuple(enabled), tuple(problems))


@dataclass(frozen=True)
class Bar:
    """One stored day of prices (spec §6). ``close`` is the first close the desk
    saw for the day; ``split_ratio`` is new shares per old share from that day;
    ``dividend`` is per share on its ex-date; ``source`` is ``yahoo`` or ``ansaar``."""

    day: date
    close: float | None
    split_ratio: float | None = None
    dividend: float | None = None
    source: str = "yahoo"


@dataclass(frozen=True)
class Fill:
    symbol: str
    asset_class: str
    side: str  # "buy" | "sell"
    qty: int
    price: float
    costs: float
    day: date


@dataclass
class Lot:
    qty: float
    cost: float  # per share, buy costs included
    opened: date


@dataclass(frozen=True)
class Realised:
    day: date
    symbol: str
    asset_class: str
    gain: float
    long_term: bool


@dataclass
class Book:
    """Cash, FIFO lots and realised gains, rebuilt from fills by :func:`replay`."""

    cash: float
    lots: dict[str, list[Lot]] = field(default_factory=dict)
    classes: dict[str, str] = field(default_factory=dict)
    realised: list[Realised] = field(default_factory=list)
    dividends: float = 0.0

    def qty(self, symbol: str) -> float:
        return sum(lot.qty for lot in self.lots.get(symbol, ()))

    def held(self) -> dict[str, float]:
        return {s: q for s in self.lots if (q := self.qty(s)) > _EPS}

    def held_classes(self) -> set[str]:
        return {self.classes[s] for s in self.held()}

    def avg_cost(self, symbol: str) -> float:
        lots = self.lots.get(symbol, [])
        qty = sum(lot.qty for lot in lots)
        return sum(lot.qty * lot.cost for lot in lots) / qty if qty > _EPS else 0.0


def bar_on(series: list[Bar], day: date) -> Bar | None:
    """The bar dated exactly ``day``, or None. ``series`` is sorted by day."""
    i = bisect_right(series, day, key=lambda b: b.day)
    return series[i - 1] if i and series[i - 1].day == day else None


def close_on(series: list[Bar], day: date) -> float | None:
    """The last known close on or before ``day``."""
    i = bisect_right(series, day, key=lambda b: b.day)
    while i > 0:
        i -= 1
        if series[i].close is not None:
            return series[i].close
    return None


def _long_term(opened: date, sold: date) -> bool:
    """Held for more than 12 months: the long-term test for listed shares and units."""
    try:
        anniversary = opened.replace(year=opened.year + 1)
    except ValueError:  # 29 February
        anniversary = opened.replace(year=opened.year + 1, day=28)
    return sold > anniversary


def replay(fills: list[Fill], bars: dict[str, list[Bar]], capital: float, upto: date) -> Book:
    """The desk at the end of ``upto``: every fill and corporate action on or before it.

    One day's events go in the order they happen at a broker: a split or bonus
    (effective from the open), then a dividend (paid to the previous close's
    holders), then sells, then buys.
    """
    book = Book(cash=capital)
    by_day: dict[date, list[Fill]] = defaultdict(list)
    for f in fills:
        if f.day <= upto:
            by_day[f.day].append(f)
    event_days = {
        b.day for series in bars.values() for b in series if b.day <= upto and (b.split_ratio or b.dividend)
    }
    for day in sorted(set(by_day) | event_days):
        for symbol in list(book.lots):
            bar = bar_on(bars.get(symbol, []), day)
            if bar is None:
                continue
            if bar.split_ratio and bar.split_ratio != 1:
                _split(book, symbol, bar.split_ratio, bar.close)
            if bar.dividend:
                paid = book.qty(symbol) * bar.dividend
                book.cash += paid
                book.dividends += paid
        for f in sorted(by_day.get(day, []), key=lambda f: f.side != "sell"):
            book.classes[f.symbol] = f.asset_class
            if f.side == "buy":
                book.cash -= f.qty * f.price + f.costs
                book.lots.setdefault(f.symbol, []).append(
                    Lot(float(f.qty), (f.qty * f.price + f.costs) / f.qty, f.day)
                )
            else:
                book.cash += f.qty * f.price - f.costs
                _consume(book, f)
    return book


def _split(book: Book, symbol: str, ratio: float, close: float | None) -> None:
    lots = book.lots[symbol]
    for lot in lots:
        lot.qty *= ratio
        lot.cost /= ratio
    total = sum(lot.qty for lot in lots)
    frac = total - math.floor(total + _EPS)
    if frac > _EPS:
        # A broker pays cash for a fractional entitlement. `ponytail:` taken off
        # the newest lot with no realised gain; it is a fraction of one share.
        lots[-1].qty -= frac
        book.cash += frac * (close or 0.0)
        book.lots[symbol] = [lot for lot in lots if lot.qty > _EPS]


def _consume(book: Book, f: Fill) -> None:
    """Take ``f``'s shares off the oldest lots first (FIFO) and book each gain."""
    lots = book.lots.get(f.symbol, [])
    net = (f.qty * f.price - f.costs) / f.qty
    left = float(f.qty)
    while left > _EPS and lots:
        lot = lots[0]
        take = min(lot.qty, left)
        book.realised.append(
            Realised(f.day, f.symbol, f.asset_class, take * (net - lot.cost), _long_term(lot.opened, f.day))
        )
        lot.qty -= take
        left -= take
        if lot.qty <= _EPS:
            lots.pop(0)
    if not lots:
        book.lots.pop(f.symbol, None)


def value(book: Book, bars: dict[str, list[Bar]], day: date) -> float:
    """Cash plus holdings at the last close on or before ``day``. A holding with
    no price at all counts at its cost; the daily run raises desk_price_missing."""
    total = book.cash
    for symbol, qty in book.held().items():
        px = close_on(bars.get(symbol, []), day)
        total += qty * (px if px is not None else book.avg_cost(symbol))
    return total


def fy(day: date) -> int:
    """The Indian financial year (April-March) a day falls in, by its first year."""
    return day.year if day.month >= 4 else day.year - 1


def tax_owed(realised: list[Realised], rules: Rules) -> float:
    """Tax on realised gains, per financial year (spec §7). `ponytail:`
    conservative: no netting across classes and no loss carry-forward."""
    worst = max(rules.tax_rate.values(), default=0.30)
    years: dict[int, list[Realised]] = defaultdict(list)
    for r in realised:
        years[fy(r.day)].append(r)
    total = 0.0
    for rows in years.values():
        short: dict[str, float] = defaultdict(float)
        long_gain: dict[str, float] = defaultdict(float)
        for r in rows:
            (long_gain if r.long_term else short)[r.asset_class] += r.gain
        total += sum(rules.tax_rate.get(c, worst) * max(0.0, g) for c, g in short.items())
        for asset_class, gain in long_gain.items():
            # The ₹1.25L exemption is section 112A: listed equity and
            # equity-oriented units. A gold or silver ETF gets none of it.
            free = rules.ltcg_exemption_inr if asset_class == "equity" else 0.0
            total += rules.ltcg_rate * max(0.0, gain - free)
    return total


@dataclass(frozen=True)
class Order:
    """A planned order, sized at the decision-date close (spec §4)."""

    symbol: str
    asset_class: str
    side: str
    qty: int
    ref_price: float


@dataclass(frozen=True)
class PendingOrder:
    id: str
    symbol: str
    asset_class: str
    side: str
    qty: int
    created_day: date
    data_date: date
    seq: int


@dataclass(frozen=True)
class FillResult:
    order_id: str
    status: str  # "filled" | "cancelled" | "pending"
    fill_day: date | None = None
    qty: int = 0
    price: float | None = None
    costs: float = 0.0
    source: str | None = None
    reason: str = ""


def _costs(side: str, qty: int, price: float, rules: Rules) -> float:
    fee = qty * price * rules.cost_pct_per_side
    return fee + rules.sell_charge_inr if side == "sell" else fee


def plan_orders(
    rows: tuple[Decision, ...],
    book: Book,
    closes: dict[str, float],
    rules: Rules,
    *,
    frozen: frozenset[str] = frozenset(),
    cash_reserved: float = 0.0,
) -> tuple[list[Order], list[str]]:
    """Orders that move ``book`` toward ``rows``, in the order they must fill (spec §4).

    ``closes`` holds the decision-date close of every held and decided symbol
    the desk has a price for. Returns the orders and the names skipped, each as
    ``"SYMBOL: reason"``.

    ``frozen`` names symbols the desk may not trade today, because an order for
    each is still pending: a second order would race the first. They are skipped
    as ``pending_order`` and still count toward the portfolio value, because the
    desk still owns them. ``cash_reserved`` is the money those pending buys will
    spend, which today's buys therefore may not.
    """
    held = book.held()
    port = book.cash + sum(q * closes.get(s, book.avg_cost(s)) for s, q in held.items())
    wanted = {r.symbol: r for r in rows}
    sells: list[Order] = []
    buys: list[tuple[tuple[int, float, str], Order]] = []
    skipped: list[str] = []
    for symbol, qty in held.items():
        if symbol in wanted:
            continue
        if symbol in frozen:
            skipped.append(f"{symbol}: pending_order")
            continue
        px = closes.get(symbol)
        if not px:
            skipped.append(f"{symbol}: no_price")
            continue
        n = math.floor(qty + _EPS)
        if n > 0:
            sells.append(Order(symbol, book.classes[symbol], "sell", n, px))
    for r in rows:
        if r.symbol in frozen:
            skipped.append(f"{r.symbol}: pending_order")
            continue
        px = closes.get(r.symbol)
        if not px:
            skipped.append(f"{r.symbol}: no_price")
            continue
        target = r.target_weight * port
        have = held.get(r.symbol, 0.0)
        gap = target - have * px
        if have > _EPS and abs(gap) <= max(rules.band_abs * port, rules.band_rel * target):
            continue
        if gap > 0:
            n = math.floor(gap / px + _EPS)
            if n == 0:
                skipped.append(f"{r.symbol}: below_one_share")
                continue
            key = (r.selection_rank, -r.target_weight, r.symbol)
            buys.append((key, Order(r.symbol, r.asset_class, "buy", n, px)))
        elif gap < 0:
            n = min(math.floor(have + _EPS), int(-gap / px + 0.5))
            if n > 0:
                sells.append(Order(r.symbol, r.asset_class, "sell", n, px))
    cash = (
        book.cash
        - cash_reserved
        + sum(o.qty * o.ref_price - _costs("sell", o.qty, o.ref_price, rules) for o in sells)
    )
    orders = list(sells)
    for _, o in sorted(buys, key=lambda item: item[0]):
        unit = o.ref_price * (1 + rules.cost_pct_per_side)
        n = min(o.qty, math.floor(cash / unit + _EPS)) if cash > 0 else 0
        if n == 0:
            skipped.append(f"{o.symbol}: no_cash")
            continue
        orders.append(Order(o.symbol, o.asset_class, "buy", n, o.ref_price))
        cash -= n * unit
    return orders, skipped


def _split_factor(series: list[Bar], after: date, upto: date) -> float:
    """New shares per old share from splits dated after ``after``, up to ``upto``."""
    factor = 1.0
    for b in series:
        if after < b.day <= upto and b.split_ratio:
            factor *= b.split_ratio
    return factor


def fill_orders(
    pending: list[PendingOrder],
    bars: dict[str, list[Bar]],
    index_days: list[date],
    book: Book,
    rules: Rules,
    grace_days: int = 3,
) -> list[FillResult]:
    """Fill pending paper orders at the close of their fill day (spec §6).

    The fill day is the first market day on or after the day an order was
    created. ``book`` is the desk before these fills. Sells fill before buys, and
    ``seq`` orders each side; a buy that no longer fits the cash is cut, or cancelled
    as ``no_cash``. Sells of one name share the holding, so each is capped by what
    the earlier ones left. No price ``grace_days`` market days after the fill day
    cancels the order as ``price_missing``.
    """
    days = sorted(index_days)
    cash = book.cash
    sold: dict[str, int] = defaultdict(int)
    results: list[FillResult] = []
    for o in sorted(pending, key=lambda o: (o.created_day, o.side != "sell", o.seq)):
        fill_day = next((d for d in days if d >= o.created_day), None)
        if fill_day is None:
            results.append(FillResult(o.id, "pending"))
            continue
        series = bars.get(o.symbol, [])
        bar = bar_on(series, fill_day)
        if bar is None or bar.close is None:
            late = sum(1 for d in days if d > fill_day) >= grace_days
            results.append(
                FillResult(o.id, "cancelled", reason="price_missing") if late else FillResult(o.id, "pending")
            )
            continue
        px = bar.close
        qty = math.floor(o.qty * _split_factor(series, o.data_date, fill_day) + _EPS)
        if o.side == "sell":
            qty = min(qty, math.floor(book.qty(o.symbol) + _EPS) - sold[o.symbol])
            if qty <= 0:
                results.append(FillResult(o.id, "cancelled", reason="nothing_held"))
                continue
            sold[o.symbol] += qty
            costs = _costs("sell", qty, px, rules)
            cash += qty * px - costs
        else:
            unit = px * (1 + rules.cost_pct_per_side)
            qty = min(qty, math.floor(cash / unit + _EPS)) if cash > 0 else 0
            if qty <= 0:
                results.append(FillResult(o.id, "cancelled", reason="no_cash"))
                continue
            costs = _costs("buy", qty, px, rules)
            cash -= qty * px + costs
        results.append(FillResult(o.id, "filled", fill_day, qty, px, costs, bar.source))
    return results


MIN_WEEKS = 12


@dataclass(frozen=True)
class Stats:
    n: int
    mean: float
    sd: float
    t: float


def desk_values(
    fills: list[Fill], bars: dict[str, list[Bar]], capital: float, days: list[date]
) -> list[tuple[date, float]]:
    """The desk's value at each day's close. `ponytail:` replays from scratch
    per day, O(days x fills); fine for years of a daily desk."""
    return [(d, value(replay(fills, bars, capital, d), bars, d)) for d in days]


def benchmark_values(
    series: list[Bar], capital: float, cost_pct: float, days: list[date]
) -> list[tuple[date, float]]:
    """``capital`` put into one instrument at the first day's close, paying one
    buy cost, then held: splits adjust the units and dividends go to cash (spec §8)."""
    if not days:
        return []
    start = close_on(series, days[0])
    if not start:
        return []
    units = capital * (1 - cost_pct) / start
    cash = 0.0
    last = days[0]
    out: list[tuple[date, float]] = []
    for d in days:
        for b in series:
            if last < b.day <= d:
                if b.split_ratio:
                    units *= b.split_ratio
                if b.dividend:
                    cash += units * b.dividend
        last = max(last, d)
        out.append((d, units * (close_on(series, d) or 0.0) + cash))
    return out


def _week_ends(values: list[tuple[date, float]]) -> dict[tuple[int, int], float]:
    ends: dict[tuple[int, int], float] = {}
    for d, v in values:  # in date order, so the week's last day wins
        iso = d.isocalendar()
        ends[(iso.year, iso.week)] = v
    return ends


def weekly_excess(desk: list[tuple[date, float]], bench: list[tuple[date, float]]) -> list[float]:
    """The desk's return minus the benchmark's, one per ISO week, from each
    week's last value to the next (spec §8)."""
    a, b = _week_ends(desk), _week_ends(bench)
    weeks = sorted(set(a) & set(b))
    return [
        (a[cur] / a[prev] - 1) - (b[cur] / b[prev] - 1)
        for prev, cur in zip(weeks, weeks[1:], strict=False)
        if a[prev] and b[prev]
    ]


def stats(xs: list[float]) -> Stats:
    n = len(xs)
    if n < 2:
        return Stats(n, xs[0] if xs else 0.0, 0.0, 0.0)
    mean = statistics.fmean(xs)
    sd = statistics.stdev(xs)
    return Stats(n, mean, sd, mean / sd * math.sqrt(n) if sd > 0 else 0.0)


def label(s: Stats) -> str:
    """How much weight to give the result, in words (spec §8)."""
    if s.n < MIN_WEEKS:
        return "too early"
    if s.t <= -2:
        return "clearly behind the benchmark"
    if s.t <= -1:
        return "behind the benchmark"
    if s.t < 1:
        return "no evidence yet"
    if s.t < 2:
        return "suggestive"
    return "strong"


def below_expectation(s: Stats, expected_excess_pa: float) -> bool:
    """True when live results are more than two standard errors below the
    weekly excess the backtest implies (spec §8)."""
    if s.n < MIN_WEEKS:
        return False
    weekly = (1 + expected_excess_pa) ** (1 / 52) - 1
    return s.mean + 2 * s.sd / math.sqrt(s.n) < weekly


def big_moves(
    bars: dict[str, list[Bar]], symbols: set[str], start: date, end: date
) -> list[tuple[str, date, float]]:
    """Closes that moved more than 50% from the previous close, inside
    ``start``..``end``, with no split recorded that day: likely a bad price."""
    out: list[tuple[str, date, float]] = []
    for symbol in sorted(symbols):
        prev: float | None = None
        for b in bars.get(symbol, []):
            if b.close is None:
                continue
            if prev and start <= b.day <= end and not b.split_ratio:
                move = b.close / prev - 1
                if abs(move) > 0.5:
                    out.append((symbol, b.day, move))
            prev = b.close
    return out
