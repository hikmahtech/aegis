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
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_EPS = 1e-9
_UTC = ZoneInfo("UTC")

# Three knobs used to carry a currency in their name. A deployment that has not
# been re-saved since still holds the old keys, so `from_config` reads either,
# new name first. Dropping the old ones would have put a live desk on a zero
# sell charge and a zero exemption the moment the new code deployed, minutes
# before anyone could rewrite the row.
RENAMED = {
    "sell_charge": "sell_charge_inr",
    "long_term_rate": "ltcg_rate",
    "long_term_exemption": "ltcg_exemption_inr",
}


@dataclass(frozen=True)
class Rules:
    """The desk's knobs (spec §12). ``from_config`` merges an activities row's
    ``config`` over these defaults, so a missing key is never an error.

    **The defaults name no market, no currency and no tax law.** AEGIS is
    forked and configured for someone else's life, so one operator's exchange,
    tickers and tax rates belong in their own ``trading-desk-daily`` row, with
    an example setup in ``config/seed/activities.yaml``. A desk with no
    ``calendar_symbol`` cannot tell a market day from a holiday, so it does
    nothing at all rather than guessing a market.
    """

    mode: str = "paper"

    # --- the market ---------------------------------------------------------
    # The instrument whose bars ARE the trading calendar, in the price source's
    # own naming (Yahoo: "^NSEI", "^GSPC", "^FTSE"). Empty means unconfigured.
    calendar_symbol: str = ""
    # The clock the desk reads "today" from: an IANA zone name.
    market_tz: str = "UTC"
    # What the price source appends to a plain instrument symbol to say which
    # exchange it trades on: ".NS" for the NSE, ".L" for London, ".TO" for
    # Toronto. US listings need none, which is why empty is the default.
    symbol_suffix: str = ""
    # The ISO code every figure on this desk is in. It is the whole desk's
    # currency, so the charges and the exemption below no longer carry one in
    # their names.
    currency: str = ""
    # The month the financial year starts in, for grouping realised gains: 1 is
    # the calendar year, 4 an April-to-March year.
    fy_start_month: int = 1
    # Calendar days before the trading calendar itself looks wrong. A long
    # weekend plus a holiday is 4 to 5 days in most markets; a market with a
    # longer normal closure needs this raised, or it will alarm every year.
    stale_calendar_days: int = 6
    # Calendar days before a price is too old to size or sell on.
    stale_price_days: int = 7

    # --- the money ----------------------------------------------------------
    capital: float = 0.0
    asset_classes: tuple[str, ...] = ("equity", "etf")
    cost_pct_per_side: float = 0.002
    # A flat charge on every sell, in `currency`. Was `sell_charge_inr`.
    sell_charge: float = 0.0
    band_abs: float = 0.02
    band_rel: float = 0.25
    max_order_pct: float = 0.25
    # Which print an order fills at: "open" (the session's first trade, the
    # earliest price a signal from the previous close could actually have
    # bought) or "close". Defaults to "close" because that is what every desk
    # did before this knob existed, so a deployment that has not opted in keeps
    # its arithmetic. Filling at the open only makes sense when the desk plans
    # BEFORE its market opens — `trading_desk._tick` enforces that, and without
    # it an order would fill at a price struck before it was decided.
    fill_at: str = "close"

    # --- the tax model ------------------------------------------------------
    # Short-term rate per asset class. Empty means no tax model is configured,
    # and then the desk deducts nothing and says so rather than inventing a
    # rate. Was `ltcg_rate` and `ltcg_exemption_inr`.
    tax_rate: dict[str, float] = field(default_factory=dict)
    long_term_rate: float = 0.0
    long_term_exemption: float = 0.0
    # Which asset classes the long-term exemption applies to. India's section
    # 112A covers listed equity and equity-oriented units and not a gold ETF,
    # so which classes qualify is the operator's law to state, not ours.
    long_term_exemption_classes: tuple[str, ...] = ()

    # --- the score ----------------------------------------------------------
    benchmark: str = ""
    context_benchmark: str = ""
    expected_excess_pa: float = 0.0
    # Where a benchmark's prices can also come from, when Yahoo has no close for
    # a market day: benchmark name (Yahoo's form) to the NSE symbol and asset
    # class ansaar wants, e.g. {"SHARIABEES.NS": {"symbol": "SHARIABEES",
    # "asset_class": "etf"}}. Empty by default and an unmapped benchmark simply
    # gets no fallback, so this repo ships nobody's tickers.
    benchmark_prices: dict[str, dict[str, str]] = field(default_factory=dict)

    def configured(self) -> bool:
        """Whether the desk knows which market it trades. Without a trading
        calendar it cannot tell a market day from a holiday, and guessing one
        would place real paper orders on a day the exchange was shut."""
        return bool(self.calendar_symbol)

    def tz(self) -> ZoneInfo:
        """The clock the desk reads "today" from.

        An unknown zone name falls back to UTC rather than stopping the run:
        reading is forgiving and writing is strict, the same split the email
        triage rules use. `desk_rules.validate` refuses a bad name with a 400,
        so a typo is caught where someone is watching."""
        try:
            return ZoneInfo(self.market_tz)
        except (ZoneInfoNotFoundError, ValueError):
            return _UTC

    def price_symbol(self, symbol: str) -> str:
        """An instrument's name at the price source (``TCS`` to ``TCS.NS``).

        An index (``^NSEI``) and a symbol that already names its exchange are
        left alone, so a benchmark can be configured in the source's own form.
        With no suffix configured, every symbol is already in that form."""
        if not self.symbol_suffix or symbol.startswith("^") or "." in symbol:
            return symbol
        return f"{symbol}{self.symbol_suffix}"

    @classmethod
    def from_config(cls, cfg: dict | None) -> Rules:
        cfg = cfg or {}
        base = cls()

        def raw(key: str) -> Any:
            """The configured value, under the current name or the old one."""
            value = cfg.get(key)
            return cfg.get(RENAMED[key]) if value is None and key in RENAMED else value

        def num(key: str) -> float:
            value = raw(key)
            return float(value) if value is not None else float(getattr(base, key))

        def whole(key: str) -> int:
            value = cfg.get(key)
            return int(value) if value is not None else int(getattr(base, key))

        def text(key: str) -> str:
            return str(cfg.get(key) or getattr(base, key))

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
            mode=text("mode"),
            calendar_symbol=text("calendar_symbol"),
            market_tz=text("market_tz"),
            symbol_suffix=text("symbol_suffix"),
            currency=text("currency").upper(),
            fy_start_month=whole("fy_start_month"),
            stale_calendar_days=whole("stale_calendar_days"),
            stale_price_days=whole("stale_price_days"),
            capital=num("capital"),
            asset_classes=tuple(cfg.get("asset_classes") or base.asset_classes),
            cost_pct_per_side=num("cost_pct_per_side"),
            sell_charge=num("sell_charge"),
            band_abs=num("band_abs"),
            band_rel=num("band_rel"),
            max_order_pct=num("max_order_pct"),
            # Read leniently, like every other key here. `fill_price_on` treats
            # anything that is not "open" as the close, so a typo degrades to
            # the old behaviour rather than stopping the desk trading. The
            # strict check lives at the write boundary, in `desk_rules.validate`.
            fill_at=text("fill_at").lower(),
            tax_rate=rates,
            long_term_rate=num("long_term_rate"),
            long_term_exemption=num("long_term_exemption"),
            long_term_exemption_classes=tuple(cfg.get("long_term_exemption_classes") or ()),
            benchmark=text("benchmark"),
            context_benchmark=text("context_benchmark"),
            expected_excess_pa=num("expected_excess_pa"),
            benchmark_prices=benches,
        )


def legacy_keys(cfg: dict | None) -> list[str]:
    """The old, currency-suffixed key names a config row still supplies a value
    under, and which nothing newer overrides.

    `from_config` reads them so a deployment keeps working the moment the new
    code lands — `schedule_sync` re-reads this row every few minutes, so there
    is no ordering of deploy and config write that avoids the gap. This names
    them so the gap is visible: the daily run logs it and the admin page says
    which settings are still stored under a retired name."""
    cfg = cfg or {}
    return sorted(old for new, old in RENAMED.items() if cfg.get(new) is None and cfg.get(old) is not None)


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


def market_days(index_bars: list[Bar], today: date) -> list[date]:
    """The sessions the market has completed, read off the calendar index's bars.

    A day counts when it has a close, or when it is before ``today`` and has an
    open. Yahoo sometimes serves a day the market plainly traded with both
    fields null, even a day later, while the desk kept that day's open from its
    own post-open run. An open is proof the session happened, and once the day
    is over it has ended, so it is a market day whatever Yahoo says now (#667).

    Today still needs a close. With an open alone it has not ended, has no
    decisions yet, and must never become the day the desk plans."""
    return [
        b.day for b in index_bars
        if b.close is not None or (b.open is not None and b.day < today)
    ]


def last_trading_day(index_days: list[date], today: date) -> date | None:
    """The latest market day strictly before ``today``, from the index's bars."""
    before = [d for d in index_days if d < today]
    return max(before) if before else None


def trading_week(market_days: Iterable[date]) -> set[int]:
    """Which days of the week this market trades on, read off its own bars.

    The shape of a week is part of a market, and the desk's market is
    configuration: most exchanges trade Monday to Friday, a few Sunday to
    Thursday. The calendar's own bars say which, so nothing here has to assume
    anyone's weekend."""
    return {d.weekday() for d in market_days}


def last_expected_day(today: date, week: set[int]) -> date | None:
    """The most recent day strictly before ``today`` that this market would
    normally have traded on. None when its week is not known yet."""
    if not week:
        return None
    day = today - timedelta(days=1)
    for _ in range(7):
        if day.weekday() in week:
            return day
        day -= timedelta(days=1)
    return None


def idle_days(market_days: set[date], first: date, last: date) -> list[date]:
    """Days from ``first`` to ``last`` this market would normally have traded on
    and has no bar for.

    Two things look exactly like this and the desk cannot tell them apart, so it
    counts both: a market holiday, and a day the price source simply did not
    serve. The index's bars ARE the desk's calendar, which is why one of these
    is normal and a run of them is not (#525)."""
    week = trading_week(market_days)
    out: list[date] = []
    day = first
    while day <= last:
        if day.weekday() in week and day not in market_days:
            out.append(day)
        day += timedelta(days=1)
    return out


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
    ``dividend`` is per share on its ex-date; ``source`` is ``yahoo`` or ``ansaar``.

    ``open`` is the session's first print, and it sits LAST rather than beside
    ``close`` where it reads better. Every field here is ``float | None`` or
    defaulted, and ``trading_desk._bars`` builds this positionally, so a field
    inserted mid-struct would silently load ``split_ratio`` into ``open`` and
    ``source`` into ``dividend`` without raising anything. Appending is the only
    placement the type system cannot catch getting wrong.
    """

    day: date
    close: float | None
    split_ratio: float | None = None
    dividend: float | None = None
    source: str = "yahoo"
    open: float | None = None


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


def open_on(series: list[Bar], day: date) -> float | None:
    """The open dated EXACTLY ``day``, or None.

    Deliberately not "the last known open on or before", the way `close_on`
    works. A close carries forward because it is the best mark available for a
    day the market did not price; an open does not, because it is a statement
    about one session's first trade. Carrying yesterday's open into today would
    fill an order at a price from a day that has closed."""
    bar = bar_on(series, day)
    return bar.open if bar else None


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
    """Cash plus holdings at the last close on or before ``day``.

    A holding whose price has gone dark keeps its LAST CLOSE, however old: that
    is what ``close_on`` returns, and the daily run raises `desk_price_missing`
    for it rather than writing it down. Cost is used only for a holding with no
    stored close at all on or before ``day``, which a filled order normally
    rules out. So a delisted position overstates the desk's value at its final
    traded price until someone sells it (#524)."""
    total = book.cash
    for symbol, qty in book.held().items():
        px = close_on(bars.get(symbol, []), day)
        total += qty * (px if px is not None else book.avg_cost(symbol))
    return total


def fy(day: date, start_month: int = 1) -> int:
    """The financial year a day falls in, named by the calendar year it starts
    in. ``start_month`` 1 is the calendar year; 4 an April-to-March year."""
    return day.year if day.month >= start_month else day.year - 1


def tax_owed(realised: list[Realised], rules: Rules) -> float:
    """Tax on realised gains, per financial year (spec §7). `ponytail:`
    conservative: no netting across classes and no loss carry-forward.

    With no tax model configured every rate is zero, so this returns nothing
    owed. That is the honest answer to "what are this desk's taxes" when nobody
    has said what they are, and the page labels it as unconfigured rather than
    showing a zero that reads like a result."""
    worst = max(rules.tax_rate.values(), default=0.0)
    years: dict[int, list[Realised]] = defaultdict(list)
    for r in realised:
        years[fy(r.day, rules.fy_start_month)].append(r)
    total = 0.0
    for rows in years.values():
        short: dict[str, float] = defaultdict(float)
        long_gain: dict[str, float] = defaultdict(float)
        for r in rows:
            (long_gain if r.long_term else short)[r.asset_class] += r.gain
        total += sum(rules.tax_rate.get(c, worst) * max(0.0, g) for c, g in short.items())
        for asset_class, gain in long_gain.items():
            # Which classes the exemption covers is the operator's tax law, not
            # ours: India's section 112A covers listed equity and equity-oriented
            # units, so a gold or silver ETF gets none of it. A fork whose law
            # says otherwise lists its own classes.
            free = rules.long_term_exemption if asset_class in rules.long_term_exemption_classes else 0.0
            total += rules.long_term_rate * max(0.0, gain - free)
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
    # Which print the fill got: "open", or "close" when the open was missing and
    # the desk fell back. Empty on a result that did not fill.
    kind: str = ""


def _costs(side: str, qty: int, price: float, rules: Rules) -> float:
    fee = qty * price * rules.cost_pct_per_side
    return fee + rules.sell_charge if side == "sell" else fee


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


def fill_price_on(series: list[Bar], day: date, rules: Rules) -> tuple[float | None, str]:
    """The price an order fills at on ``day``, and which print it is.

    Under ``fill_at = "open"`` the desk pays the session's first price, which is
    what a signal taken from the previous close could actually have bought. The
    close is kept as a fallback rather than a cancel: an order the desk meant to
    place should not be dropped because one source never published one field,
    and a fill at the close is exactly what this desk did until now. Anything
    other than ``"open"`` means the close, so a typo in config degrades to the
    old behaviour instead of refusing to trade.
    """
    bar = bar_on(series, day)
    if bar is None:
        return None, ""
    if rules.fill_at == "open" and bar.open is not None:
        return bar.open, "open"
    return (bar.close, "close") if bar.close is not None else (None, "")


def fill_orders(
    pending: list[PendingOrder],
    bars: dict[str, list[Bar]],
    tradable_days: list[date],
    book: Book,
    rules: Rules,
    grace_days: int = 3,
) -> list[FillResult]:
    """Fill pending paper orders on their fill day (spec §6).

    The fill day is the first tradable day on or after the day an order was
    created, and the price is `fill_price_on` — the open under `fill_at="open"`,
    else the close. ``book`` is the desk before these fills. Sells fill before
    buys, and ``seq`` orders each side; a buy that no longer fits the cash is
    cut, or cancelled as ``no_cash``. Sells of one name share the holding, so
    each is capped by what the earlier ones left. No price ``grace_days``
    tradable days after the fill day cancels the order as ``price_missing``.

    ``tradable_days`` is NOT the same list as the calendar the rest of the desk
    runs on. It may include today, once today has an open — that is the whole
    point of filling at the open. The calendar that decides which day's
    decisions to act on stays strictly behind today, because a session that has
    not closed has no decisions yet.
    """
    days = sorted(tradable_days)
    cash = book.cash
    sold: dict[str, int] = defaultdict(int)
    results: list[FillResult] = []
    for o in sorted(pending, key=lambda o: (o.created_day, o.side != "sell", o.seq)):
        fill_day = next((d for d in days if d >= o.created_day), None)
        if fill_day is None:
            results.append(FillResult(o.id, "pending"))
            continue
        series = bars.get(o.symbol, [])
        px, print_used = fill_price_on(series, fill_day, rules)
        if px is None:
            late = sum(1 for d in days if d > fill_day) >= grace_days
            results.append(
                FillResult(o.id, "cancelled", reason="price_missing") if late else FillResult(o.id, "pending")
            )
            continue
        bar = bar_on(series, fill_day)
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
        results.append(
            FillResult(o.id, "filled", fill_day, qty, px, costs, bar.source if bar else None, kind=print_used)
        )
    return results


MIN_WEEKS = 12


@dataclass(frozen=True)
class Stats:
    n: int
    mean: float
    sd: float
    t: float


def desk_series(
    fills: list[Fill], bars: dict[str, list[Bar]], capital: float, days: list[date]
) -> list[tuple[date, float, float]]:
    """Each day's close: the desk's value, and the share of it that is invested.

    The invested share is what the §8 alarm needs. The desk holds roughly the
    pipeline's own heat and keeps the rest in cash, while the benchmark holds
    the whole capital, so a gap measured against the full benchmark is mostly
    that difference in exposure.

    `ponytail:` replays from scratch per day, O(days x fills); fine for years of
    a daily desk."""
    out: list[tuple[date, float, float]] = []
    for d in days:
        book = replay(fills, bars, capital, d)
        total = value(book, bars, d)
        out.append((d, total, (total - book.cash) / total if total else 0.0))
    return out


def desk_values(
    fills: list[Fill], bars: dict[str, list[Bar]], capital: float, days: list[date]
) -> list[tuple[date, float]]:
    """The desk's value at each day's close."""
    return [(d, v) for d, v, _ in desk_series(fills, bars, capital, days)]


def benchmark_values(
    series: list[Bar], capital: float, cost_pct: float, days: list[date]
) -> list[tuple[date, float]]:
    """``capital`` put into one instrument on the first day, paying one buy cost,
    then held: splits adjust the units and dividends go to cash (spec §8).

    It buys at that day's OPEN when there is one, because the desk it is scored
    against does. The whole point of the benchmark is "what the same money would
    have earned doing nothing clever", and that comparison is only honest if
    both sides enter on the same print — otherwise the gap carries a fixed
    slice of one session's move that has nothing to do with the picking. Falls
    back to the close, which is what every day before this change has."""
    if not days:
        return []
    start = open_on(series, days[0]) or close_on(series, days[0])
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


MIN_INVESTED = 0.01  # a week entered with almost nothing at risk says nothing about the picking


def _paired_weeks(
    a: dict[tuple[int, int], float],
    b: dict[tuple[int, int], float],
    s: dict[tuple[int, int], float] | None = None,
) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """Consecutive ISO weeks every series has a value for, as (previous, current).

    With ``s``, a week the desk entered with almost nothing invested is left
    out: its return per rupee at risk is a tiny number over a tiny number."""
    weeks = sorted(set(a) & set(b) & (set(a) if s is None else set(s)))
    return [
        (prev, cur)
        for prev, cur in zip(weeks, weeks[1:], strict=False)
        if a[prev] and b[prev] and (s is None or s[prev] >= MIN_INVESTED)
    ]


def weekly_excess(
    desk: list[tuple[date, float]],
    bench: list[tuple[date, float]],
    shares: list[tuple[date, float]] | None = None,
) -> list[float]:
    """The desk's return minus the benchmark's, one per ISO week, from each
    week's last value to the next (spec §8).

    Without ``shares`` this is the gap to the whole benchmark: what the owner
    would have earned putting the same money into SHARIABEES instead. That is
    the honest headline, and it is what the monthly section reports first.

    With ``shares`` — the desk's invested share per day — the gap is measured per
    rupee the desk actually had at risk: the desk's return over the share it
    carried into the week, minus the benchmark's. Cash earns nothing, so that
    ratio is the return on the invested part, and the gap is the stock picking
    with the cash the pipeline is deliberately not investing taken out.

    That is what the §8 alarm is judged on, for two reasons. Against the full
    benchmark a roughly a third-invested desk falls behind in any rising market
    whatever it picks, so the alarm would fire on exposure alone. And
    ``expected_excess_pa`` comes from a backtest of a fully invested book, so
    only a per-rupee figure is the same kind of number. Subtracting a scaled
    benchmark without dividing would be a third of the truth and fail a desk
    that delivers exactly what the backtest promised."""
    a, b = _week_ends(desk), _week_ends(bench)
    s = _week_ends(shares) if shares is not None else None
    return [
        (a[cur] / a[prev] - 1) / (1.0 if s is None else s[prev]) - (b[cur] / b[prev] - 1)
        for prev, cur in _paired_weeks(a, b, s)
    ]


def weekly_shares(
    desk: list[tuple[date, float]], bench: list[tuple[date, float]], shares: list[tuple[date, float]]
) -> list[float]:
    """The invested share the desk carried into each week ``weekly_excess``
    measures, so the report can say what the scaling was."""
    a, b, s = _week_ends(desk), _week_ends(bench), _week_ends(shares)
    return [s[prev] for prev, _ in _paired_weeks(a, b, s)]


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
    weekly excess the backtest implies (spec §8).

    Give it the invested-scaled statistics, not the gap to the whole benchmark:
    the backtest is of a fully invested book, and the desk is not, so the full
    gap would fail this on cash drag alone."""
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
