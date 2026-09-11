"""The trading desk's arithmetic: pure functions, no I/O.

Spec: docs/superpowers/specs/2026-09-12-maou-trading-desk-design.md (§4-§8).
Everything here takes plain values and returns plain values, so every rule the
spec states is one unit test away. `trading_desk.py` does the reading and writing.

Money is float. `ponytail:` this is a score, not the ledger; Decimal matters
where a live fill is posted to the books, which is the live spec's job.
"""

from __future__ import annotations

import math
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

    @classmethod
    def from_config(cls, cfg: dict | None) -> Rules:
        cfg = cfg or {}
        base = cls()

        def num(key: str) -> float:
            raw = cfg.get(key)
            return float(raw) if raw is not None else float(getattr(base, key))

        rates = {k: float(v) for k, v in (cfg.get("tax_rate") or {}).items()}
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

    ``outcome`` is ``ok``, ``held_stale`` or ``held_suspect``. ``rows`` is what
    may trade. ``problems`` are plain-English lines for ``desk_decisions_suspect``,
    and can be non-empty on an ``ok`` day: a bad row was dropped and the rest traded.
    """

    outcome: str
    rows: tuple[Decision, ...]
    problems: tuple[str, ...]


def last_trading_day(index_days: list[date], today: date) -> date | None:
    """The latest market day strictly before ``today``, from the index's bars."""
    before = [d for d in index_days if d < today]
    return max(before) if before else None


def check_decisions(rows: list[Decision], held_classes: set[str], rules: Rules) -> Check:
    """Spec §5. An empty day is stale; odd weights or a held class that vanished
    for no stated reason hold the whole day; a non-halal or short row is dropped."""
    if not rows:
        return Check("held_stale", (), ())
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
