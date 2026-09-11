# Maou's Trading Desk (Paper Mode) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Maou copies the trading system's daily `trade_decisions`, sizes them into whole-share
paper orders, fills them at Yahoo's close, and reports the result against SHARIABEES in the
monthly close. It refuses stale or odd input and raises a `#money` task instead.

**Architecture:** The arithmetic is pure functions in `core/src/aegis/services/desk_math.py`.
Reading and writing live in `core/src/aegis/services/trading_desk.py`. Two connectors fetch the
data: `AnsaarClient` for decisions and `FinanceConnector.daily_bars` for Yahoo prices. One Temporal
activity, `TradingDeskActivities.desk_tick`, runs the day, and a thin `TradingDeskFlow` schedules
it. Problems go through `hub_watch.reconcile_findings`. The monthly close gets a new section.

**Tech Stack:** Python 3.12, asyncpg (Postgres 16), httpx, Temporal Python SDK, pytest with
pytest-asyncio (`asyncio_mode = "auto"`), and respx.

**Spec:** `docs/superpowers/specs/2026-09-12-maou-trading-desk-design.md`. Read it first; this plan
argues from it.

**Out of scope:**
- Grading decisions: the pipeline does that (trading-system-pipeline #356).
- The post-mortem and weekly-review lane, which is a later AEGIS spec.
- Live Kite orders (spec §15).

## Global Constraints

- **No model.** The desk computes every number in code; the model writes nothing on it (spec §2).
- **Prices.** Yahoo first, ansaar as fallback, and `price_source` records which. **A stored close is
  never overwritten** (`COALESCE(existing, new)`). No bar dated today or later is stored. No
  adjusted closes anywhere (spec §3 step 3, §8).
- **Defaults** (spec §12), read from the `trading-desk-daily` activities row's `config`, merged
  over code defaults:
  - `mode: paper`, `capital: 100000`, `asset_classes: [equity, etf]`
  - `cost_pct_per_side: 0.002`, `sell_charge_inr: 16`
  - `band_abs: 0.02`, `band_rel: 0.25`, `max_order_pct: 0.25`
  - `tax_rate: {equity: 0.20, etf: 0.30}`, `ltcg_rate: 0.125`, `ltcg_exemption_inr: 125000`
  - `benchmark: SHARIABEES.NS`, `context_benchmark: ^NSEI`, `expected_excess_pa: 0.06`
- **Problems:**
  - `source = "money"`, `subject_kind = "trading_desk"`.
  - Daily classes: `desk_decisions_stale`, `desk_decisions_suspect`, `desk_source_error`,
    `desk_price_missing`.
  - Monthly class: `desk_below_expectation`, reconciled only by the monthly close.
- **Secrets.** Never log, print or put `ansaar_service_secret` in an error message.
- **Migration** is `migrations/045_trading_desk.sql`. If another PR takes 045 before this merges,
  renumber it. Idempotent DDL only (`IF NOT EXISTS`).
- **Tests** run against a real Postgres, with no DB mocks. Fake HTTP clients must take the real
  methods' parameters, and a test checks that. Run tests with `flock` and `tee` exactly as each
  step shows. The log goes to `logs/` in the worktree, which is git-ignored.
- **Lint and formatting.** `ruff check` must pass for every package touched. Never run
  `ruff format` on `core/src/aegis/services/chat.py` or `core/src/aegis/services/tools/infra.py`.
- **Writing.** Commit subjects are single-line semantic, e.g. `feat(desk): ...`. Code comments and
  docstrings are plain English, following the surrounding code's density.
- **Timezone.** Every date is an IST calendar date: `ZoneInfo("Asia/Kolkata")`.

**Test command** (substitute the paths; run from the worktree root):

```bash
mkdir -p logs && flock /tmp/aegis-pytest.lock -c 'PYTHONPATH=core/src:worker/src:comms/src /home/arshad/Workspace/hikmah/aegis/.venv/bin/pytest <PATHS> -n auto --dist loadfile --timeout=300' 2>&1 | tee logs/<NAME>.log
```

**Lint command:**

```bash
/home/arshad/Workspace/hikmah/aegis/.venv/bin/ruff check core/src/ tests/core/ worker/src/ tests/worker/
```

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `core/src/aegis/services/desk_math.py` (create) | Pure arithmetic: rules, checks, replay (lots, splits, dividends, FIFO), value, tax, order sizing, fills, scoring | 1, 2, 3, 4 |
| `core/src/aegis/connectors/finance.py` (modify) | `FinanceConnector.daily_bars`: Yahoo daily bars plus split and dividend events | 5 |
| `core/src/aegis/connectors/ansaar.py` (create) | `AnsaarClient` (client token, `decisions`, `prices`) and `AnsaarError` | 5 |
| `migrations/045_trading_desk.sql` (create) | `finance.desk_decisions`, `desk_plans`, `desk_orders`, `desk_prices` | 6 |
| `core/src/aegis/services/trading_desk.py` (create) | Storage, `run_tick`, `month_summary`, `reconcile_expectation` | 6, 7 |
| `worker/src/aegis_worker/activities/money.py` (modify) | `build_month_close` adds `close["desk"]` and runs the monthly check | 7 |
| `worker/src/aegis_worker/activities/money_render.py` (modify) | `desk_lines` and the desk section in `render_month_close` | 7 |
| `core/src/aegis/config.py`, `core/src/aegis/services/integrations_config.py` (modify) | `ansaar_url` and `ansaar_service_secret` settings and admin keys; `read_integration` | 8 |
| `worker/src/aegis_worker/activities/trading_desk.py` (create) | `TradingDeskActivities.desk_tick` | 8 |
| `worker/src/aegis_worker/flows/trading_desk.py` (create) | `TradingDeskFlow`, `TradingDeskConfig` | 8 |
| `worker/src/aegis_worker/registry.py`, `worker/src/aegis_worker/__main__.py`, `config/seed/activities.yaml` (modify) | Registration, construction, seed row | 8 |
| `CLAUDE.md` (modify) | One paragraph describing the lane | 8 |

---

### Task 1: Desk rules and the checks before trading

**Files:**
- Create: `core/src/aegis/services/desk_math.py`
- Test: `tests/core/services/test_desk_math_checks.py`

**Interfaces:**
- Produces:
  - `Rules` (frozen dataclass) with `Rules.from_config(cfg: dict | None) -> Rules`
  - `Decision(symbol, asset_class, halal_status, direction, target_weight, selection_rank, recovery_state="NORMAL", active_kill_conditions="")`
  - `Check(outcome: str, rows: tuple[Decision, ...], problems: tuple[str, ...])`
  - `last_trading_day(index_days: list[date], today: date) -> date | None`
  - `check_decisions(rows: list[Decision], held_classes: set[str], rules: Rules) -> Check`
  - The module constants `DEFAULT_TAX_RATE` and `_EPS`.

- [ ] **Step 1: Write the failing tests**

```python
"""The desk's rules and its checks before trading (spec §5, §12)."""

from __future__ import annotations

from datetime import date

from aegis.services.desk_math import Decision, Rules, check_decisions, last_trading_day


def d(symbol, weight, cls="equity", rank=1, halal="COMPLIANT", direction="LONG", state="NORMAL", kill=""):
    return Decision(symbol, cls, halal, direction, weight, rank, state, kill)


def test_rules_from_config_merges_over_the_defaults():
    r = Rules.from_config({"capital": 50000, "tax_rate": {"etf": 0.2}, "asset_classes": ["equity"]})
    assert r.capital == 50000.0
    assert r.tax_rate == {"equity": 0.20, "etf": 0.2}
    assert r.asset_classes == ("equity",)
    assert r.band_abs == 0.02 and r.mode == "paper" and r.benchmark == "SHARIABEES.NS"


def test_rules_from_no_config_are_the_defaults():
    assert Rules.from_config(None) == Rules()
    assert Rules.from_config({}) == Rules()


def test_last_trading_day_is_strictly_before_today():
    days = [date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 14)]
    assert last_trading_day(days, date(2026, 9, 14)) == date(2026, 9, 11)
    assert last_trading_day(days, date(2026, 9, 15)) == date(2026, 9, 14)
    assert last_trading_day(days, date(2026, 9, 10)) is None


def test_an_empty_day_is_stale():
    c = check_decisions([], set(), Rules())
    assert (c.outcome, c.rows, c.problems) == ("held_stale", (), ())


def test_a_clean_day_passes_and_leaves_out_disabled_classes():
    rows = [d("TCS", 0.1), d("GOLDBEES", 0.1, cls="etf"), d("BTCUSDT", 0.1, cls="crypto")]
    c = check_decisions(rows, set(), Rules())
    assert c.outcome == "ok"
    assert [r.symbol for r in c.rows] == ["TCS", "GOLDBEES"]
    assert c.problems == ()


def test_a_non_compliant_row_is_dropped_and_the_rest_trades():
    rows = [d("TCS", 0.1), d("XYZ", 0.1, halal="NON_COMPLIANT")]
    c = check_decisions(rows, set(), Rules())
    assert c.outcome == "ok"
    assert [r.symbol for r in c.rows] == ["TCS"]
    assert len(c.problems) == 1 and "XYZ" in c.problems[0]


def test_a_short_row_is_dropped_too():
    c = check_decisions([d("TCS", 0.1, direction="SHORT")], set(), Rules())
    assert c.rows == () and "TCS" in c.problems[0]


def test_weights_over_the_whole_portfolio_are_suspect():
    c = check_decisions([d(f"S{i}", 0.2) for i in range(6)], set(), Rules())
    assert c.outcome == "held_suspect" and c.rows == ()
    assert "120.0%" in c.problems[0]


def test_a_weight_above_the_order_cap_is_suspect():
    c = check_decisions([d("TCS", 0.30)], set(), Rules())
    assert c.outcome == "held_suspect" and "TCS" in c.problems[0]


def test_a_zero_weight_is_suspect():
    assert check_decisions([d("TCS", 0.0)], set(), Rules()).outcome == "held_suspect"


def test_a_held_class_that_vanishes_without_a_reason_is_suspect():
    c = check_decisions([d("GOLDBEES", 0.1, cls="etf")], {"equity"}, Rules())
    assert c.outcome == "held_suspect" and "equity" in c.problems[0]


def test_a_vanished_class_is_fine_when_a_kill_switch_explains_it():
    rows = [d("GOLDBEES", 0.1, cls="etf", kill="VIX_HIGH")]
    assert check_decisions(rows, {"equity"}, Rules()).outcome == "ok"


def test_a_vanished_class_is_fine_in_a_recovery_state():
    rows = [d("GOLDBEES", 0.1, cls="etf", state="OBSERVATION")]
    assert check_decisions(rows, {"equity"}, Rules()).outcome == "ok"


def test_a_held_class_the_desk_no_longer_trades_is_not_suspect():
    rules = Rules.from_config({"asset_classes": ["equity"]})
    assert check_decisions([d("TCS", 0.1)], {"etf"}, rules).outcome == "ok"
```

- [ ] **Step 2: Run the tests; they must fail with `ModuleNotFoundError: No module named 'aegis.services.desk_math'`**

Run the test command with `<PATHS>` = `tests/core/services/test_desk_math_checks.py`, `<NAME>` = `desk-t1`.

- [ ] **Step 3: Write the module**

```python
"""The trading desk's arithmetic: pure functions, no I/O.

Spec: docs/superpowers/specs/2026-09-12-maou-trading-desk-design.md (§4-§8).
Everything here takes plain values and returns plain values, so every rule the
spec states is one unit test away. `trading_desk.py` does the reading and writing.

Money is float. `ponytail:` this is a score, not the ledger; Decimal matters
where a live fill is posted to the books, which is the live spec's job.
"""

from __future__ import annotations

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
```

- [ ] **Step 4: Run the tests; all must pass.** Same command; the log goes to `logs/desk-t1.log`.

- [ ] **Step 5: Prove the tests can fail.** Change `if total > 1.0 + 1e-6 or bad:` to
  `if bad:` and rerun. `test_weights_over_the_whole_portfolio_are_suspect` must fail. Restore the
  line and rerun until green.

- [ ] **Step 6: Lint and commit**

```bash
/home/arshad/Workspace/hikmah/aegis/.venv/bin/ruff check core/src/aegis/services/desk_math.py tests/core/services/test_desk_math_checks.py
git add core/src/aegis/services/desk_math.py tests/core/services/test_desk_math_checks.py
git commit -m "feat(desk): the trading desk's rules and its checks before trading"
```

---

### Task 2: Replay the book: lots, splits, dividends, FIFO, value and tax

**Files:**
- Modify: `core/src/aegis/services/desk_math.py`. Append after `check_decisions`, and add the
  imports named below.
- Test: `tests/core/services/test_desk_math_book.py`

**Interfaces:**
- Consumes: `Rules` and `_EPS` from Task 1.
- Produces:
  - `Bar(day, close, split_ratio=None, dividend=None, source="yahoo")`
  - `Fill(symbol, asset_class, side, qty: int, price: float, costs: float, day)`
  - `Lot(qty: float, cost: float, opened: date)`
  - `Realised(day, symbol, asset_class, gain: float, long_term: bool)`
  - `Book(cash, lots, classes, realised, dividends)` with `.qty(symbol)`, `.held() -> dict[str, float]`,
    `.held_classes() -> set[str]` and `.avg_cost(symbol)`
  - `bar_on(series, day) -> Bar | None`
  - `close_on(series, day) -> float | None`
  - `replay(fills, bars: dict[str, list[Bar]], capital, upto) -> Book`
  - `value(book, bars, day) -> float`
  - `fy(day) -> int`
  - `tax_owed(realised, rules) -> float`
  - `bars` always maps the desk's symbol (NSE form) to a list sorted by `day`.

- [ ] **Step 1: Write the failing tests**

```python
"""Replaying the desk's fills into a book (spec §6, §7)."""

from __future__ import annotations

from datetime import date

import pytest

from aegis.services.desk_math import (
    Bar,
    Book,
    Fill,
    Lot,
    Realised,
    Rules,
    close_on,
    fy,
    replay,
    tax_owed,
    value,
)


def test_buy_then_partial_sell_books_cash_lots_and_a_short_term_gain():
    fills = [
        Fill("TCS", "equity", "buy", 10, 1000.0, 20.0, date(2026, 9, 15)),
        Fill("TCS", "equity", "sell", 4, 1100.0, 24.8, date(2026, 9, 22)),
    ]
    book = replay(fills, {}, 100_000.0, date(2026, 9, 30))
    assert book.cash == pytest.approx(94_355.2)
    assert book.qty("TCS") == pytest.approx(6)
    [r] = book.realised
    assert r.gain == pytest.approx(367.2) and not r.long_term
    assert book.held_classes() == {"equity"}


def test_fills_after_upto_are_ignored():
    fills = [Fill("TCS", "equity", "buy", 1, 100.0, 0.0, date(2026, 9, 15))]
    assert replay(fills, {}, 1000.0, date(2026, 9, 14)).cash == 1000.0


def test_fifo_takes_the_oldest_lot_first_and_splits_short_from_long():
    fills = [
        Fill("A", "equity", "buy", 10, 100.0, 0.0, date(2025, 1, 10)),
        Fill("A", "equity", "buy", 10, 200.0, 0.0, date(2025, 6, 10)),
        Fill("A", "equity", "sell", 15, 300.0, 0.0, date(2026, 1, 15)),
    ]
    book = replay(fills, {}, 10_000.0, date(2026, 1, 31))
    assert [(r.gain, r.long_term) for r in book.realised] == [(2000.0, True), (500.0, False)]
    assert book.qty("A") == 5


def test_held_exactly_twelve_months_is_still_short_term():
    fills = [
        Fill("A", "equity", "buy", 1, 100.0, 0.0, date(2025, 3, 1)),
        Fill("A", "equity", "sell", 1, 150.0, 0.0, date(2026, 3, 1)),
    ]
    assert not replay(fills, {}, 1000.0, date(2026, 3, 2)).realised[0].long_term


def test_a_split_doubles_the_quantity_and_halves_the_cost():
    bars = {"B": [Bar(date(2026, 9, 20), 510.0, split_ratio=2.0)]}
    fills = [
        Fill("B", "equity", "buy", 10, 1000.0, 0.0, date(2026, 9, 15)),
        Fill("B", "equity", "sell", 20, 520.0, 0.0, date(2026, 9, 25)),
    ]
    before = replay(fills[:1], bars, 20_000.0, date(2026, 9, 21))
    assert before.qty("B") == 20 and before.lots["B"][0].cost == 500.0
    after = replay(fills, bars, 20_000.0, date(2026, 9, 30))
    assert after.realised[0].gain == pytest.approx(400.0)
    assert after.qty("B") == 0 and "B" not in after.lots


def test_a_fractional_split_entitlement_is_paid_in_cash():
    bars = {"C": [Bar(date(2026, 9, 20), 100.0, split_ratio=1.5)]}
    fills = [Fill("C", "equity", "buy", 5, 150.0, 0.0, date(2026, 9, 15))]
    book = replay(fills, bars, 1000.0, date(2026, 9, 21))
    assert book.qty("C") == 7
    assert book.cash == pytest.approx(1000.0 - 750.0 + 0.5 * 100.0)


def test_a_dividend_is_paid_on_the_ex_date_to_the_shares_held():
    bars = {"D": [Bar(date(2026, 9, 18), 100.0, dividend=5.0)]}
    fills = [Fill("D", "equity", "buy", 10, 100.0, 0.0, date(2026, 9, 15))]
    book = replay(fills, bars, 2000.0, date(2026, 9, 30))
    assert book.cash == pytest.approx(2000.0 - 1000.0 + 50.0)
    assert book.dividends == pytest.approx(50.0)


def test_shares_bought_on_the_ex_date_get_no_dividend():
    bars = {"D": [Bar(date(2026, 9, 18), 100.0, dividend=5.0)]}
    fills = [Fill("D", "equity", "buy", 10, 100.0, 0.0, date(2026, 9, 18))]
    assert replay(fills, bars, 2000.0, date(2026, 9, 30)).dividends == 0.0


def test_close_on_takes_the_last_known_close():
    series = [Bar(date(2026, 9, 15), 1000.0), Bar(date(2026, 9, 16), None), Bar(date(2026, 9, 18), 1050.0)]
    assert close_on(series, date(2026, 9, 14)) is None
    assert close_on(series, date(2026, 9, 16)) == 1000.0
    assert close_on(series, date(2026, 9, 17)) == 1000.0
    assert close_on(series, date(2026, 9, 18)) == 1050.0


def test_value_marks_holdings_at_the_last_close_on_or_before_the_day():
    bars = {"TCS": [Bar(date(2026, 9, 15), 1000.0), Bar(date(2026, 9, 16), None), Bar(date(2026, 9, 17), 1050.0)]}
    fills = [Fill("TCS", "equity", "buy", 10, 1000.0, 0.0, date(2026, 9, 15))]
    book = replay(fills, bars, 20_000.0, date(2026, 9, 16))
    assert value(book, bars, date(2026, 9, 16)) == pytest.approx(20_000.0)
    assert value(book, bars, date(2026, 9, 17)) == pytest.approx(10_000.0 + 10_500.0)


def test_value_falls_back_to_cost_when_a_holding_has_no_price():
    book = replay([Fill("X", "equity", "buy", 2, 500.0, 10.0, date(2026, 9, 15))], {}, 5000.0, date(2026, 9, 16))
    assert value(book, {}, date(2026, 9, 16)) == pytest.approx(5000.0)


def test_book_helpers():
    book = Book(cash=0.0, lots={"A": [Lot(2, 100.0, date(2026, 1, 1)), Lot(2, 200.0, date(2026, 2, 1))]}, classes={"A": "etf"})
    assert book.avg_cost("A") == 150.0 and book.held() == {"A": 4} and book.held_classes() == {"etf"}
    assert book.avg_cost("NONE") == 0.0


def test_financial_year_runs_april_to_march():
    assert fy(date(2026, 3, 31)) == 2025
    assert fy(date(2026, 4, 1)) == 2026


def _r(day, gain, cls="equity", lt=False):
    return Realised(day, "S", cls, gain, lt)


def test_tax_nets_short_term_gains_per_class_within_a_year():
    rows = [_r(date(2026, 5, 1), 1000.0), _r(date(2026, 6, 1), -400.0), _r(date(2026, 7, 1), 500.0, cls="etf")]
    assert tax_owed(rows, Rules()) == pytest.approx(0.20 * 600 + 0.30 * 500)


def test_tax_does_not_net_across_the_31_march_boundary():
    rows = [_r(date(2026, 3, 31), 1000.0), _r(date(2026, 4, 1), -1000.0)]
    assert tax_owed(rows, Rules()) == pytest.approx(200.0)


def test_long_term_gains_are_taxed_only_above_the_exemption():
    assert tax_owed([_r(date(2026, 5, 1), 200_000.0, lt=True)], Rules()) == pytest.approx(0.125 * 75_000)


def test_a_loss_year_owes_nothing():
    assert tax_owed([_r(date(2026, 5, 1), -500.0)], Rules()) == 0.0


def test_an_unknown_class_pays_the_highest_configured_rate():
    assert tax_owed([_r(date(2026, 5, 1), 100.0, cls="crypto")], Rules()) == pytest.approx(30.0)
```

- [ ] **Step 2: Run the tests; they must fail with `ImportError` on `Bar`.** Use `<PATHS>` = `tests/core/services/test_desk_math_book.py`, `<NAME>` = `desk-t2`.

- [ ] **Step 3: Implement.** Extend the imports at the top of `desk_math.py` to:

```python
import math
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
```

Then append:

```python
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
        long_gain = 0.0
        for r in rows:
            if r.long_term:
                long_gain += r.gain
            else:
                short[r.asset_class] += r.gain
        total += sum(rules.tax_rate.get(c, worst) * max(0.0, g) for c, g in short.items())
        total += rules.ltcg_rate * max(0.0, long_gain - rules.ltcg_exemption_inr)
    return total
```

- [ ] **Step 4: Run the tests; all must pass.** Also rerun Task 1's file, to catch anything
  the import change broke.

- [ ] **Step 5: Prove the tests can fail.** In `replay`, change the fill sort key to
  `lambda f: f.side == "sell"`, which puts buys first. Only
  `test_buy_then_partial_sell_books_cash_lots_and_a_short_term_gain` spans two days, and none of
  these tests puts a buy and a sell on the same day. So instead break `_long_term` to
  `return sold >= anniversary`. `test_held_exactly_twelve_months_is_still_short_term` must fail.
  Restore it.

- [ ] **Step 6: Lint and commit**

```bash
git add core/src/aegis/services/desk_math.py tests/core/services/test_desk_math_book.py
git commit -m "feat(desk): replay fills into a book with splits, dividends, FIFO gains and tax"
```

---

### Task 3: Sizing orders and paper fills

**Files:**
- Modify: `core/src/aegis/services/desk_math.py` (append)
- Test: `tests/core/services/test_desk_math_orders.py`

**Interfaces:**
- Consumes: `Rules`, `Decision`, `Bar`, `Book`, `Lot`, `bar_on` and `_EPS` from Tasks 1 and 2.
- Produces:
  - `Order(symbol, asset_class, side, qty: int, ref_price: float)`
  - `PendingOrder(id: str, symbol, asset_class, side, qty: int, created_day: date, data_date: date, seq: int)`
  - `FillResult(order_id, status, fill_day=None, qty=0, price=None, costs=0.0, source=None, reason="")`
    with `status` one of `filled`, `cancelled` or `pending`.
  - `plan_orders(rows: tuple[Decision, ...], book: Book, closes: dict[str, float], rules) -> tuple[list[Order], list[str]]`
    returns the orders in fill order (sells, then buys by rank) and the skips as `"SYMBOL: reason"`.
  - `fill_orders(pending: list[PendingOrder], bars: dict[str, list[Bar]], index_days: list[date], book: Book, rules) -> list[FillResult]`

- [ ] **Step 1: Write the failing tests**

```python
"""Sizing orders (spec §4) and paper fills (spec §6)."""

from __future__ import annotations

from datetime import date

import pytest

from aegis.services.desk_math import (
    Bar,
    Book,
    Decision,
    Lot,
    PendingOrder,
    Rules,
    fill_orders,
    plan_orders,
)


def d(symbol, weight, cls="equity", rank=1):
    return Decision(symbol, cls, "COMPLIANT", "LONG", weight, rank)


def held(symbol, qty, cost, cls="equity", cash=0.0):
    book = Book(cash=cash)
    book.lots[symbol] = [Lot(float(qty), cost, date(2026, 9, 1))]
    book.classes[symbol] = cls
    return book


def test_first_plan_buys_whole_shares_and_skips_a_share_too_dear():
    rows = (d("TCS", 0.10, rank=1), d("INFY", 0.10, rank=2), d("MRF", 0.10, rank=3))
    closes = {"TCS": 3000.0, "INFY": 1500.0, "MRF": 130_000.0}
    orders, skipped = plan_orders(rows, Book(cash=100_000.0), closes, Rules())
    assert [(o.symbol, o.side, o.qty, o.ref_price) for o in orders] == [
        ("TCS", "buy", 3, 3000.0),
        ("INFY", "buy", 6, 1500.0),
    ]
    assert skipped == ["MRF: below_one_share"]


def test_a_small_gap_inside_the_band_is_not_traded():
    book = held("TCS", 3, 3000.0, cash=91_000.0)
    assert plan_orders((d("TCS", 0.10),), book, {"TCS": 3000.0}, Rules()) == ([], [])


def test_a_gap_beyond_the_band_tops_up():
    book = held("TCS", 3, 3000.0, cash=91_000.0)
    orders, _ = plan_orders((d("TCS", 0.20),), book, {"TCS": 3000.0}, Rules())
    assert [(o.side, o.qty) for o in orders] == [("buy", 3)]


def test_names_not_in_the_decisions_are_sold_in_full_and_overweights_trimmed():
    book = held("TCS", 10, 3000.0, cash=40_000.0)
    book.lots["INFY"] = [Lot(20.0, 1500.0, date(2026, 9, 1))]
    book.classes["INFY"] = "equity"
    orders, _ = plan_orders((d("TCS", 0.10),), book, {"TCS": 3000.0, "INFY": 1500.0}, Rules())
    assert [(o.symbol, o.side, o.qty) for o in orders] == [("INFY", "sell", 20), ("TCS", "sell", 7)]


def test_buys_go_in_rank_order_and_the_last_one_runs_out_of_cash():
    rows = tuple(d(f"S{i}", 0.10, rank=i) for i in range(10, 0, -1))
    closes = {f"S{i}": 1000.0 for i in range(1, 11)}
    orders, skipped = plan_orders(rows, Book(cash=10_000.0), closes, Rules())
    assert [o.symbol for o in orders] == [f"S{i}" for i in range(1, 10)]
    assert skipped == ["S10: no_cash"]


def test_rank_ties_go_to_the_larger_weight_then_the_symbol():
    rows = (d("B", 0.05, cls="etf"), d("A", 0.05), d("C", 0.10))
    orders, _ = plan_orders(rows, Book(cash=100_000.0), {"A": 100.0, "B": 100.0, "C": 100.0}, Rules())
    assert [o.symbol for o in orders] == ["C", "A", "B"]


def test_a_decided_name_with_no_price_is_skipped():
    assert plan_orders((d("NEW", 0.1),), Book(cash=1000.0), {}, Rules()) == ([], ["NEW: no_price"])


def test_a_held_name_with_no_price_is_kept_and_reported():
    book = held("OLD", 5, 100.0, cash=1000.0)
    assert plan_orders((), book, {}, Rules()) == ([], ["OLD: no_price"])


DAYS = [date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18)]


def p(order_id, symbol, side, qty, created=date(2026, 9, 14), data=date(2026, 9, 11), seq=0):
    return PendingOrder(order_id, symbol, "equity", side, qty, created, data, seq)


def test_a_buy_fills_at_the_close_of_its_fill_day_with_costs():
    bars = {"TCS": [Bar(date(2026, 9, 14), 3100.0)]}
    [r] = fill_orders([p("o1", "TCS", "buy", 3)], bars, DAYS, Book(cash=100_000.0), Rules())
    assert (r.status, r.fill_day, r.qty, r.price, r.source) == ("filled", date(2026, 9, 14), 3, 3100.0, "yahoo")
    assert r.costs == pytest.approx(18.6)


def test_an_order_created_on_a_holiday_fills_on_the_next_market_day():
    bars = {"TCS": [Bar(date(2026, 9, 14), 3100.0)]}
    [r] = fill_orders([p("o1", "TCS", "buy", 1, created=date(2026, 9, 13))], bars, DAYS, Book(cash=10_000.0), Rules())
    assert r.fill_day == date(2026, 9, 14)


def test_no_price_yet_stays_pending_then_cancels_after_three_market_days():
    order = p("o1", "TCS", "buy", 1)
    [r] = fill_orders([order], {}, DAYS[:2], Book(cash=10_000.0), Rules())
    assert r.status == "pending"
    [r] = fill_orders([order], {}, DAYS, Book(cash=10_000.0), Rules())
    assert (r.status, r.reason) == ("cancelled", "price_missing")


def test_no_market_day_yet_stays_pending():
    [r] = fill_orders([p("o1", "TCS", "buy", 1, created=date(2026, 9, 19))], {}, DAYS, Book(cash=10.0), Rules())
    assert r.status == "pending"


def test_sells_fill_first_and_a_buy_is_cut_to_the_cash_left():
    book = held("X", 10, 100.0, cash=1000.0)
    bars = {"X": [Bar(date(2026, 9, 14), 100.0)], "Y": [Bar(date(2026, 9, 14), 100.0)]}
    pending = [p("buy", "Y", "buy", 20, seq=1), p("sell", "X", "sell", 10, seq=0)]
    res = {r.order_id: r for r in fill_orders(pending, bars, DAYS, book, Rules())}
    assert res["sell"].costs == pytest.approx(18.0)
    assert (res["buy"].status, res["buy"].qty) == ("filled", 19)


def test_a_buy_with_no_cash_left_is_cancelled():
    bars = {"Y": [Bar(date(2026, 9, 14), 100.0)]}
    [r] = fill_orders([p("o", "Y", "buy", 1)], bars, DAYS, Book(cash=50.0), Rules())
    assert (r.status, r.reason) == ("cancelled", "no_cash")


def test_a_split_between_sizing_and_fill_scales_the_quantity():
    bars = {"TCS": [Bar(date(2026, 9, 14), 1550.0, split_ratio=2.0)]}
    [r] = fill_orders([p("o", "TCS", "buy", 3)], bars, DAYS, Book(cash=100_000.0), Rules())
    assert r.qty == 6


def test_the_fill_records_where_its_price_came_from():
    bars = {"TCS": [Bar(date(2026, 9, 14), 3100.0, source="ansaar")]}
    [r] = fill_orders([p("o", "TCS", "buy", 1)], bars, DAYS, Book(cash=10_000.0), Rules())
    assert r.source == "ansaar"


def test_a_sell_never_exceeds_what_is_held():
    bars = {"X": [Bar(date(2026, 9, 14), 100.0)]}
    [r] = fill_orders([p("o", "X", "sell", 8)], bars, DAYS, held("X", 5, 100.0), Rules())
    assert r.qty == 5


def test_a_sell_of_nothing_held_is_cancelled():
    bars = {"X": [Bar(date(2026, 9, 14), 100.0)]}
    [r] = fill_orders([p("o", "X", "sell", 1)], bars, DAYS, Book(cash=0.0), Rules())
    assert (r.status, r.reason) == ("cancelled", "nothing_held")
```

- [ ] **Step 2: Run the tests; they must fail with `ImportError` on `PendingOrder`.** Use
  `<PATHS>` = `tests/core/services/test_desk_math_orders.py`, `<NAME>` = `desk-t3`.

- [ ] **Step 3: Implement.** Append to `desk_math.py`:

```python
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
    rows: tuple[Decision, ...], book: Book, closes: dict[str, float], rules: Rules
) -> tuple[list[Order], list[str]]:
    """Orders that move ``book`` toward ``rows``, in the order they must fill (spec §4).

    ``closes`` holds the decision-date close of every held and decided symbol
    the desk has a price for. Returns the orders and the names skipped, each as
    ``"SYMBOL: reason"``.
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
        px = closes.get(symbol)
        if not px:
            skipped.append(f"{symbol}: no_price")
            continue
        n = math.floor(qty + _EPS)
        if n > 0:
            sells.append(Order(symbol, book.classes[symbol], "sell", n, px))
    for r in rows:
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
    cash = book.cash + sum(o.qty * o.ref_price - _costs("sell", o.qty, o.ref_price, rules) for o in sells)
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
) -> list[FillResult]:
    """Fill pending paper orders at the close of their fill day (spec §6).

    The fill day is the first market day on or after the day an order was
    created. ``book`` is the desk before these fills. Orders fill in ``seq``
    order, sells first; a buy that no longer fits the cash is cut, or cancelled
    as ``no_cash``. No price three market days after the fill day cancels the
    order as ``price_missing``.
    """
    days = sorted(index_days)
    cash = book.cash
    results: list[FillResult] = []
    for o in sorted(pending, key=lambda o: (o.created_day, o.seq)):
        fill_day = next((d for d in days if d >= o.created_day), None)
        if fill_day is None:
            results.append(FillResult(o.id, "pending"))
            continue
        series = bars.get(o.symbol, [])
        bar = bar_on(series, fill_day)
        if bar is None or bar.close is None:
            late = sum(1 for d in days if d > fill_day) >= 3
            results.append(
                FillResult(o.id, "cancelled", reason="price_missing") if late else FillResult(o.id, "pending")
            )
            continue
        px = bar.close
        qty = math.floor(o.qty * _split_factor(series, o.data_date, fill_day) + _EPS)
        if o.side == "sell":
            qty = min(qty, math.floor(book.qty(o.symbol) + _EPS))
            if qty <= 0:
                results.append(FillResult(o.id, "cancelled", reason="nothing_held"))
                continue
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
```

- [ ] **Step 4: Run the tests; all must pass.** Rerun the Task 1 and Task 2 files as well.

- [ ] **Step 5: Prove the tests can fail.** Change `max(rules.band_abs * port, rules.band_rel * target)`
  to `0`. `test_a_small_gap_inside_the_band_is_not_traded` must fail. Restore it.

- [ ] **Step 6: Lint and commit**

```bash
git add core/src/aegis/services/desk_math.py tests/core/services/test_desk_math_orders.py
git commit -m "feat(desk): size decisions into whole-share orders and fill them on paper"
```

---

### Task 4: The score: value history, benchmark, weekly excess, labels

**Files:**
- Modify: `core/src/aegis/services/desk_math.py`. Append, and add `import statistics`.
- Test: `tests/core/services/test_desk_math_score.py`

**Interfaces:**
- Consumes: `Bar`, `Fill`, `replay`, `value` and `close_on` from Task 2.
- Produces:
  - `Stats(n: int, mean: float, sd: float, t: float)` and `MIN_WEEKS = 12`
  - `desk_values(fills, bars, capital, days) -> list[tuple[date, float]]`
  - `benchmark_values(series: list[Bar], capital, cost_pct, days) -> list[tuple[date, float]]`
  - `weekly_excess(desk, bench) -> list[float]`
  - `stats(xs) -> Stats`
  - `label(s: Stats) -> str`
  - `below_expectation(s: Stats, expected_excess_pa: float) -> bool`
  - `big_moves(bars, symbols: set[str], start, end) -> list[tuple[str, date, float]]`

- [ ] **Step 1: Write the failing tests**

```python
"""Scoring the desk (spec §8)."""

from __future__ import annotations

from datetime import date

import pytest

from aegis.services.desk_math import (
    Bar,
    Fill,
    Stats,
    below_expectation,
    benchmark_values,
    big_moves,
    desk_values,
    label,
    stats,
    weekly_excess,
)


def test_desk_values_mark_the_book_each_day():
    bars = {"TCS": [Bar(date(2026, 9, 14), 1000.0), Bar(date(2026, 9, 15), 1100.0)]}
    fills = [Fill("TCS", "equity", "buy", 5, 1000.0, 10.0, date(2026, 9, 14))]
    out = desk_values(fills, bars, 10_000.0, [date(2026, 9, 14), date(2026, 9, 15)])
    assert [v for _, v in out] == pytest.approx([9_990.0, 10_490.0])


def test_benchmark_holds_units_bought_at_the_start_with_one_buy_cost():
    series = [Bar(date(2026, 9, 14), 100.0), Bar(date(2026, 9, 15), 110.0)]
    out = benchmark_values(series, 10_000.0, 0.002, [date(2026, 9, 14), date(2026, 9, 15)])
    assert [v for _, v in out] == pytest.approx([9_980.0, 9_980.0 * 1.10])


def test_benchmark_applies_splits_to_units_and_pays_dividends_as_cash():
    series = [
        Bar(date(2026, 9, 14), 100.0),
        Bar(date(2026, 9, 15), 50.0, split_ratio=2.0),
        Bar(date(2026, 9, 16), 50.0, dividend=1.0),
    ]
    days = [date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)]
    assert [v for _, v in benchmark_values(series, 1000.0, 0.0, days)] == pytest.approx([1000.0, 1000.0, 1020.0])


def test_benchmark_with_no_starting_price_is_empty():
    assert benchmark_values([], 1000.0, 0.0, [date(2026, 9, 14)]) == []
    assert benchmark_values([Bar(date(2026, 9, 14), 1.0)], 1000.0, 0.0, []) == []


def test_weekly_excess_compares_week_end_values():
    desk = [(date(2026, 9, 10), 99.0), (date(2026, 9, 11), 100.0), (date(2026, 9, 18), 102.0), (date(2026, 9, 25), 101.0)]
    bench = [(date(2026, 9, 11), 100.0), (date(2026, 9, 18), 101.0), (date(2026, 9, 25), 101.0)]
    assert weekly_excess(desk, bench) == pytest.approx([0.01, 101 / 102 - 1])


def test_stats_on_a_known_series():
    s = stats([0.01, 0.03, 0.02, 0.04])
    assert s.n == 4 and s.mean == pytest.approx(0.025)
    assert s.sd == pytest.approx(0.0129099445)
    assert s.t == pytest.approx(0.025 / 0.0129099445 * 2)


def test_stats_on_too_few_points():
    assert stats([]) == Stats(0, 0.0, 0.0, 0.0)
    assert stats([0.01]) == Stats(1, 0.01, 0.0, 0.0)
    assert stats([0.01, 0.01]).t == 0.0


@pytest.mark.parametrize(
    ("n", "t", "expected"),
    [
        (11, 5.0, "too early"),
        (12, -2.0, "clearly behind the benchmark"),
        (12, -1.5, "behind the benchmark"),
        (12, -1.0, "behind the benchmark"),
        (12, 0.0, "no evidence yet"),
        (12, 0.99, "no evidence yet"),
        (12, 1.0, "suggestive"),
        (12, 1.99, "suggestive"),
        (12, 2.0, "strong"),
    ],
)
def test_label(n, t, expected):
    assert label(Stats(n, 0.0, 0.0, t)) == expected


def test_below_expectation_fires_only_when_two_standard_errors_short():
    assert below_expectation(Stats(16, -0.002, 0.004, 0.0), 0.06) is True
    assert below_expectation(Stats(16, 0.0, 0.004, 0.0), 0.06) is False
    assert below_expectation(Stats(11, -0.01, 0.004, 0.0), 0.06) is False


def test_big_moves_flag_unexplained_jumps_only():
    bars = {
        "X": [Bar(date(2026, 9, 14), 100.0), Bar(date(2026, 9, 15), 45.0), Bar(date(2026, 9, 16), 44.0)],
        "Y": [Bar(date(2026, 9, 14), 100.0), Bar(date(2026, 9, 15), 33.0, split_ratio=3.0)],
    }
    out = big_moves(bars, {"X", "Y"}, date(2026, 9, 1), date(2026, 9, 30))
    assert out == [("X", date(2026, 9, 15), pytest.approx(-0.55))]


def test_big_moves_outside_the_window_are_left_out():
    bars = {"X": [Bar(date(2026, 8, 14), 100.0), Bar(date(2026, 8, 15), 45.0)]}
    assert big_moves(bars, {"X"}, date(2026, 9, 1), date(2026, 9, 30)) == []
```

- [ ] **Step 2: Run the tests; they must fail with `ImportError` on `Stats`.** Use `<PATHS>` = `tests/core/services/test_desk_math_score.py`, `<NAME>` = `desk-t4`.

- [ ] **Step 3: Implement.** Add `import statistics` to the imports, then append:

```python
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
```

- [ ] **Step 4: Run all four `test_desk_math_*.py` files; all must pass.**

- [ ] **Step 5: Prove the tests can fail.** Change `if s.t <= -1:` to `if s.t < -1:`. The
  `(12, -1.0, "behind the benchmark")` case must fail. Restore it.

- [ ] **Step 6: Lint and commit**

```bash
git add core/src/aegis/services/desk_math.py tests/core/services/test_desk_math_score.py
git commit -m "feat(desk): score the desk against a held benchmark, weekly, with a plain label"
```

---

### Task 5: Connectors: Yahoo daily bars and the ansaar client

**Files:**
- Modify: `core/src/aegis/connectors/finance.py`. Add a method to `FinanceConnector` and extend
  the imports.
- Create: `core/src/aegis/connectors/ansaar.py`
- Test: `tests/core/connectors/test_finance_daily_bars.py`, `tests/core/connectors/test_ansaar.py`

**Interfaces:**
- Produces:
  - `FinanceConnector.daily_bars(self, symbol: str, start: date, end: date) -> list[dict]`. Each
    dict is `{"day": date, "close": float | None, "split_ratio": float | None, "dividend": float | None}`,
    oldest first. It returns `[]` on a 404 or no result, and raises `httpx.HTTPStatusError` or
    `httpx.HTTPError` otherwise.
  - `class AnsaarError(RuntimeError)`
  - `AnsaarClient(url: str, service_secret: str, *, timeout: float = 20.0, db_pool=None)`, with:
    - `async decisions(self, day: date) -> tuple[list[dict], dict]`
    - `async prices(self, symbol: str, asset_class: str, start: date, end: date) -> list[dict]`,
      oldest first, same dict shape as `daily_bars`
    - `async close()`, from `HTTPConnector`

- [ ] **Step 1: Write the failing tests**

`tests/core/connectors/test_finance_daily_bars.py`:

```python
"""Yahoo daily bars for the trading desk (spec §3 step 3)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest
import respx

from aegis.connectors.finance import FinanceConnector

CHART = r"https://query1\.finance\.yahoo\.com/v8/finance/chart/"


def _ts(day: int) -> int:
    """Yahoo stamps an NSE bar at the 09:15 IST open, 03:45 UTC."""
    return int(datetime(2026, 9, day, 3, 45, tzinfo=UTC).timestamp())


BODY = {
    "chart": {
        "result": [
            {
                "meta": {"gmtoffset": 19800},
                "timestamp": [_ts(14), _ts(15), _ts(16)],
                "events": {
                    "splits": {str(_ts(15)): {"date": _ts(15), "numerator": 2.0, "denominator": 1.0}},
                    "dividends": {str(_ts(16)): {"date": _ts(16), "amount": 1.5}},
                },
                "indicators": {"quote": [{"close": [100.0, None, 51.0]}]},
            }
        ],
        "error": None,
    }
}


@respx.mock
async def test_daily_bars_parse_closes_splits_and_dividends():
    route = respx.get(url__regex=CHART + r"TCS\.NS").mock(return_value=httpx.Response(200, json=BODY))
    bars = await FinanceConnector().daily_bars("TCS.NS", date(2026, 9, 14), date(2026, 9, 16))
    assert bars == [
        {"day": date(2026, 9, 14), "close": 100.0, "split_ratio": None, "dividend": None},
        {"day": date(2026, 9, 15), "close": None, "split_ratio": 2.0, "dividend": None},
        {"day": date(2026, 9, 16), "close": 51.0, "split_ratio": None, "dividend": 1.5},
    ]
    params = route.calls.last.request.url.params
    assert params["interval"] == "1d" and params["events"] == "div,split"
    assert int(params["period1"]) == int(datetime(2026, 9, 14, tzinfo=UTC).timestamp())
    assert int(params["period2"]) == int(datetime(2026, 9, 17, tzinfo=UTC).timestamp())


@respx.mock
async def test_daily_bars_for_an_unknown_symbol_are_empty():
    respx.get(url__regex=CHART + r"NOPE\.NS").mock(
        return_value=httpx.Response(404, json={"chart": {"result": None, "error": {"code": "Not Found"}}})
    )
    assert await FinanceConnector().daily_bars("NOPE.NS", date(2026, 9, 14), date(2026, 9, 16)) == []


@respx.mock
async def test_daily_bars_raise_on_a_server_error():
    respx.get(url__regex=CHART + r"TCS\.NS").mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await FinanceConnector().daily_bars("TCS.NS", date(2026, 9, 14), date(2026, 9, 16))
```

`tests/core/connectors/test_ansaar.py`:

```python
"""The ansaar-data client (spec §12)."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
import respx

from aegis.connectors.ansaar import AnsaarClient, AnsaarError

BASE = "http://ansaar.test"


def _token(status=200):
    body = {"success": True, "token": "t0k", "expiresIn": 900} if status == 200 else {"success": False}
    return respx.post(f"{BASE}/api/auth/client-token").mock(return_value=httpx.Response(status, json=body))


@respx.mock
async def test_decisions_fetch_a_token_once_then_the_day():
    tok = _token()
    dec = respx.get(f"{BASE}/api/execution/trade-decisions").mock(
        return_value=httpx.Response(200, json={"data": [{"symbol": "TCS"}], "meta": {"date": "2026-09-11"}})
    )
    client = AnsaarClient(BASE, "s3cret")
    rows, meta = await client.decisions(date(2026, 9, 11))
    assert rows == [{"symbol": "TCS"}] and meta == {"date": "2026-09-11"}
    assert json.loads(tok.calls.last.request.content) == {"serviceSecret": "s3cret"}
    assert dec.calls.last.request.headers["Authorization"] == "Bearer t0k"
    assert dec.calls.last.request.url.params["date"] == "2026-09-11"
    await client.decisions(date(2026, 9, 11))
    assert tok.call_count == 1
    await client.close()


@respx.mock
async def test_a_refused_token_is_an_ansaar_error_that_never_carries_the_secret():
    _token(401)
    with pytest.raises(AnsaarError) as exc:
        await AnsaarClient(BASE, "s3cret").decisions(date(2026, 9, 11))
    assert "401" in str(exc.value) and "s3cret" not in str(exc.value)


@respx.mock
async def test_a_network_error_is_an_ansaar_error():
    respx.post(f"{BASE}/api/auth/client-token").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(AnsaarError):
        await AnsaarClient(BASE, "s").decisions(date(2026, 9, 11))


@respx.mock
async def test_a_bad_status_on_the_data_call_is_an_ansaar_error():
    _token()
    respx.get(f"{BASE}/api/execution/trade-decisions").mock(return_value=httpx.Response(503))
    with pytest.raises(AnsaarError, match="503"):
        await AnsaarClient(BASE, "s").decisions(date(2026, 9, 11))


@respx.mock
async def test_prices_come_back_oldest_first_from_the_right_path():
    _token()
    eq = respx.get(f"{BASE}/api/equities/prices/M%26M").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"date": "2026-09-11", "close": 3100.5, "volume": "123"},
                    {"date": "2026-09-10", "close": 3050.0, "volume": "456"},
                    {"date": "2026-09-09", "close": None, "volume": "0"},
                ]
            },
        )
    )
    respx.get(f"{BASE}/api/etfs/GOLDBEES/prices").mock(return_value=httpx.Response(200, json={"data": [], "meta": {}}))
    client = AnsaarClient(BASE, "s")
    bars = await client.prices("M&M", "equity", date(2026, 9, 9), date(2026, 9, 11))
    assert bars == [
        {"day": date(2026, 9, 10), "close": 3050.0, "split_ratio": None, "dividend": None},
        {"day": date(2026, 9, 11), "close": 3100.5, "split_ratio": None, "dividend": None},
    ]
    assert eq.calls.last.request.url.params["from"] == "2026-09-09"
    assert eq.calls.last.request.url.params["to"] == "2026-09-11"
    assert await client.prices("GOLDBEES", "etf", date(2026, 9, 9), date(2026, 9, 11)) == []
```

- [ ] **Step 2: Run both files; they must fail.** `daily_bars` doesn't exist yet
  (`AttributeError`), and the `aegis.connectors.ansaar` module is missing. Use `<PATHS>` =
  `tests/core/connectors/test_finance_daily_bars.py tests/core/connectors/test_ansaar.py`,
  `<NAME>` = `desk-t5`.

- [ ] **Step 3a: Implement `daily_bars`.** In `core/src/aegis/connectors/finance.py`, change
  `from datetime import UTC, datetime` to `from datetime import UTC, date, datetime, timedelta`.
  Then add this method to `FinanceConnector`, after `get_overview`:

```python
    async def daily_bars(self, symbol: str, start: date, end: date) -> list[dict]:
        """Daily bars from Yahoo, oldest first, for the trading desk.

        Each bar is ``{"day", "close", "split_ratio", "dividend"}``. Yahoo rewrites
        a past ``close`` after a later split, so a caller that needs the traded
        price keeps the first value it sees (the desk does). ``[]`` when Yahoo
        has no data for the symbol; raises on any other HTTP or network error.
        """
        client = await self._ensure_client()
        t0 = time.monotonic()
        params = {
            "interval": "1d",
            "period1": int(datetime.combine(start, datetime.min.time(), UTC).timestamp()),
            "period2": int(datetime.combine(end + timedelta(days=1), datetime.min.time(), UTC).timestamp()),
            "events": "div,split",
        }
        try:
            resp = await client.get(_YAHOO_CHART_URL.format(symbol=symbol), params=params)
            if resp.status_code == 404:
                await self._record("daily_bars", "ok", int((time.monotonic() - t0) * 1000))
                return []
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            await self._record("daily_bars", "error", int((time.monotonic() - t0) * 1000), str(exc)[:200])
            raise
        await self._record("daily_bars", "ok", int((time.monotonic() - t0) * 1000))
        result = ((resp.json() or {}).get("chart") or {}).get("result") or []
        if not result:
            return []
        chart = result[0]
        offset = int((chart.get("meta") or {}).get("gmtoffset") or 0)

        def day_of(ts: object) -> date:
            return datetime.fromtimestamp(int(ts) + offset, UTC).date()

        closes = (((chart.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
        bars: dict[date, dict] = {}
        for ts, close in zip(chart.get("timestamp") or [], closes, strict=False):
            day = day_of(ts)
            bars[day] = {
                "day": day,
                "close": float(close) if close is not None else None,
                "split_ratio": None,
                "dividend": None,
            }
        events = chart.get("events") or {}
        for ev in (events.get("splits") or {}).values():
            day, den = day_of(ev["date"]), float(ev.get("denominator") or 0)
            if day in bars and den:
                bars[day]["split_ratio"] = float(ev["numerator"]) / den
        for ev in (events.get("dividends") or {}).values():
            day = day_of(ev["date"])
            if day in bars:
                bars[day]["dividend"] = float(ev["amount"])
        return [bars[d] for d in sorted(bars)]
```

- [ ] **Step 3b: Create `core/src/aegis/connectors/ansaar.py`**

```python
"""ansaar-data API client: the trading system's read side (trading desk spec §12).

Auth is a 15-minute client token from ``POST /api/auth/client-token``, given the
service secret, fetched once per client. A run makes one client, so one token.
Never the admin login (ansaar-data #25). No error message ever carries the
secret: it only travels in the token request's JSON body.
"""

from __future__ import annotations

import time
from datetime import date
from urllib.parse import quote

import httpx

from aegis.connectors._base import HTTPConnector


class AnsaarError(RuntimeError):
    """ansaar could not answer. The message is safe to show anywhere."""


class AnsaarClient(HTTPConnector):
    connector_name = "ansaar"

    def __init__(self, url: str, service_secret: str, *, timeout: float = 20.0, db_pool=None) -> None:
        super().__init__(timeout=timeout, db_pool=db_pool)
        self._url = url.rstrip("/")
        self._secret = service_secret
        self._token: str | None = None

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._url, timeout=httpx.Timeout(self._timeout, connect=5.0))

    async def _get(self, path: str, params: dict) -> dict:
        client = await self._ensure_client()
        t0 = time.monotonic()
        try:
            if self._token is None:
                resp = await client.post("/api/auth/client-token", json={"serviceSecret": self._secret})
                if resp.status_code != 200:
                    raise AnsaarError(f"client-token: HTTP {resp.status_code}")
                self._token = (resp.json() or {}).get("token")
                if not self._token:
                    raise AnsaarError("client-token: no token in the response")
            resp = await client.get(path, params=params, headers={"Authorization": f"Bearer {self._token}"})
        except httpx.HTTPError as exc:
            await self._record(path, "error", int((time.monotonic() - t0) * 1000), type(exc).__name__)
            raise AnsaarError(f"{path}: {type(exc).__name__}") from exc
        except AnsaarError as exc:
            await self._record(path, "error", int((time.monotonic() - t0) * 1000), str(exc))
            raise
        if resp.status_code != 200:
            await self._record(path, "error", int((time.monotonic() - t0) * 1000), f"HTTP {resp.status_code}")
            raise AnsaarError(f"{path}: HTTP {resp.status_code}")
        await self._record(path, "ok", int((time.monotonic() - t0) * 1000))
        return resp.json() or {}

    async def decisions(self, day: date) -> tuple[list[dict], dict]:
        """``trade_decisions`` rows for exactly ``day`` (ansaar-data #30) and the meta."""
        body = await self._get("/api/execution/trade-decisions", {"date": day.isoformat()})
        return list(body.get("data") or []), dict(body.get("meta") or {})

    async def prices(self, symbol: str, asset_class: str, start: date, end: date) -> list[dict]:
        """Closes from ansaar's own price table, oldest first: the desk's fallback
        when Yahoo has no bar. The endpoints return newest first."""
        kind = "etfs" if asset_class == "etf" else "equities"
        path = (
            f"/api/etfs/{quote(symbol, safe='')}/prices"
            if kind == "etfs"
            else f"/api/equities/prices/{quote(symbol, safe='')}"
        )
        body = await self._get(path, {"from": start.isoformat(), "to": end.isoformat(), "limit": 1000})
        out = [
            {"day": date.fromisoformat(str(r["date"])[:10]), "close": float(r["close"]), "split_ratio": None, "dividend": None}
            for r in body.get("data") or []
            if r.get("close") is not None
        ]
        return sorted(out, key=lambda b: b["day"])
```

- [ ] **Step 4: Run both files; all must pass.** Also run `tests/core/test_finance_tools.py`
  to confirm the existing quote paths still pass.

- [ ] **Step 5: Prove the tests can fail.** Remove `+ offset` from `day_of`. The bars would then
  land on the right dates anyway, because 03:45 UTC is the same calendar day. So break it
  differently: drop the `events` loop for splits. The first test must fail on
  `split_ratio`. Restore it.

- [ ] **Step 6: Lint and commit**

```bash
git add core/src/aegis/connectors/finance.py core/src/aegis/connectors/ansaar.py tests/core/connectors/test_finance_daily_bars.py tests/core/connectors/test_ansaar.py
git commit -m "feat(desk): Yahoo daily bars with splits and dividends, and an ansaar-data client"
```

---

### Task 6: The migration and the daily run

**Files:**
- Create: `migrations/045_trading_desk.sql`
- Create: `core/src/aegis/services/trading_desk.py`
- Test: `tests/core/services/test_trading_desk.py`

**Interfaces:**
- Consumes:
  - everything in `desk_math` (Tasks 1-4)
  - `AnsaarError` (Task 5)
  - `FinanceConnector.daily_bars` and `AnsaarClient.decisions/prices` signatures (Task 5)
  - `hub_watch.reconcile_findings(pool, *, source, subject_kind, classes, findings, now=None, project=True)`
- Produces, for Task 7:
  - constants `DESK_SLUG`, `INDEX`, `SOURCE`, `SUBJECT_KIND`, `DAILY_CLASSES`, `MONTHLY_CLASSES`, `MARKET_TZ`
  - `yahoo_symbol(symbol) -> str`
  - `load_rules(pool) -> Rules`
  - `_finding(klass, subject, title, description) -> dict`
  - `_bars(pool, symbols: set[str]) -> dict[str, list[Bar]]`
  - `_fills(pool) -> list[Fill]`
  - `_store_bars(pool, symbol, bars, source, today)`
  - `run_tick(pool, *, ansaar, finance, today: date | None = None, project: bool = True) -> dict`.
    The returned dict carries `today`, and may carry `day`, `skipped`, `filled`, `planned` and
    `findings`, the sorted list of finding classes.

- [ ] **Step 1: Write the migration** (`migrations/045_trading_desk.sql`)

```sql
-- Maou's trading desk, paper mode (spec docs/superpowers/specs/2026-09-12-maou-trading-desk-design.md §11).
-- Positions, cash, lots, value history and tax are derived from these on read, never stored.

-- The trading system's decisions exactly as served. Never updated: the record
-- stays true even if the pipeline later rewrites its history.
CREATE TABLE IF NOT EXISTS finance.desk_decisions (
    data_date date NOT NULL,
    symbol text NOT NULL,
    asset_class text NOT NULL,
    halal_status text NOT NULL,
    direction text NOT NULL,
    target_weight numeric NOT NULL,
    confidence numeric,
    selection_rank integer,
    recovery_state text,
    active_kill_conditions text,
    drawdown_scalar numeric,
    ml_model_version text,
    source_updated_at timestamptz,
    copied_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (data_date, symbol)
);

-- One row per decision date the desk acted on, whatever the outcome, holding
-- the findings its step produced so a rerun re-raises exactly those.
CREATE TABLE IF NOT EXISTS finance.desk_plans (
    data_date date PRIMARY KEY,
    mode text NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('orders', 'no_change', 'held_stale', 'held_suspect')),
    findings jsonb NOT NULL DEFAULT '[]'::jsonb,
    skipped jsonb NOT NULL DEFAULT '[]'::jsonb,
    planned_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS finance.desk_orders (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    mode text NOT NULL CHECK (mode IN ('paper', 'live')),
    data_date date NOT NULL REFERENCES finance.desk_plans (data_date),
    seq smallint NOT NULL,
    created_day date NOT NULL,
    symbol text NOT NULL,
    asset_class text NOT NULL,
    side text NOT NULL CHECK (side IN ('buy', 'sell')),
    qty integer NOT NULL CHECK (qty > 0),
    ref_price numeric NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'filled', 'cancelled')),
    fill_date date,
    fill_price numeric,
    costs numeric,
    price_source text,
    reason text,
    broker_order_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    filled_at timestamptz
);
CREATE INDEX IF NOT EXISTS desk_orders_pending ON finance.desk_orders (created_day, seq)
    WHERE status = 'pending';

-- Daily bars under Yahoo's symbol form. `close` is the FIRST close seen for the
-- day and is never overwritten: Yahoo rewrites past closes after a split.
CREATE TABLE IF NOT EXISTS finance.desk_prices (
    symbol text NOT NULL,
    date date NOT NULL,
    close numeric,
    split_ratio numeric,
    dividend numeric,
    source text NOT NULL,
    PRIMARY KEY (symbol, date)
);
```

- [ ] **Step 2: Write the failing tests** (`tests/core/services/test_trading_desk.py`)

```python
"""The trading desk's daily run against a real Postgres (spec §3, §5, §10)."""

from __future__ import annotations

import inspect
from datetime import date

import httpx
import pytest
import pytest_asyncio

from aegis.connectors.ansaar import AnsaarClient, AnsaarError
from aegis.connectors.finance import FinanceConnector
from aegis.services import trading_desk as td

THU, FRI, MON, TUE, WED = (date(2026, 9, d) for d in (10, 11, 14, 15, 16))

_WIPE = (
    "DELETE FROM finance.desk_orders",
    "DELETE FROM finance.desk_plans",
    "DELETE FROM finance.desk_decisions",
    "DELETE FROM finance.desk_prices",
    "DELETE FROM problem_events WHERE problem_id IN (SELECT id FROM problems WHERE subject_kind = 'trading_desk')",
    "DELETE FROM problems WHERE subject_kind = 'trading_desk'",
)


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    for sql in _WIPE:
        await db_pool.execute(sql)
    original = await db_pool.fetchrow("SELECT config FROM activities WHERE slug = $1", td.DESK_SLUG)
    yield db_pool
    for sql in _WIPE:
        await db_pool.execute(sql)
    if original is None:
        await db_pool.execute("DELETE FROM activities WHERE slug = $1", td.DESK_SLUG)
    else:
        await db_pool.execute("UPDATE activities SET config = $2 WHERE slug = $1", td.DESK_SLUG, original["config"])


def bar(day, close, split=None, div=None):
    return {"day": day, "close": close, "split_ratio": split, "dividend": div}


def row(symbol, weight, day=FRI, cls="equity", rank=1, halal="COMPLIANT", state="NORMAL", kill=""):
    """A decision as ansaar-data #30 serves it."""
    return {
        "data_date": day.isoformat(), "symbol": symbol, "asset_class": cls, "halal_status": halal,
        "direction": "LONG", "combined_forecast": 0.01, "confidence": 0.7, "target_weight": weight,
        "selection_rank": rank, "selection_score": 1.0, "drawdown_scalar": 1.0,
        "vol_scalar_portfolio": 1.0, "active_kill_conditions": kill, "recovery_state": state,
        "regime_label": "BULL_TREND", "ml_model_version": "v170", "was_held_previous": 0,
        "updated_at": f"{day.isoformat()} 17:30:11.358",
    }


class FakeFinance:
    """FinanceConnector.daily_bars with canned bars; the same parameters as the real one."""

    def __init__(self, bars=None, fail=False):
        self.bars = bars or {}
        self.fail = fail

    async def daily_bars(self, symbol, start, end):
        if self.fail:
            raise httpx.ConnectError("yahoo is down")
        return [b for b in self.bars.get(symbol, []) if start <= b["day"] <= end]


class FakeAnsaar:
    """AnsaarClient.decisions and .prices with canned data; the same parameters as the real ones."""

    def __init__(self, days=None, fail=False, prices=None):
        self.days = days or {}
        self.fail = fail
        self.price_rows = prices or {}

    async def decisions(self, day):
        if self.fail:
            raise AnsaarError("/api/execution/trade-decisions: ConnectError")
        return list(self.days.get(day, [])), {"date": day.isoformat()}

    async def prices(self, symbol, asset_class, start, end):
        return [b for b in self.price_rows.get(symbol, []) if start <= b["day"] <= end]


def test_the_fakes_take_the_real_parameters():
    for fake, real, name in (
        (FakeFinance, FinanceConnector, "daily_bars"),
        (FakeAnsaar, AnsaarClient, "decisions"),
        (FakeAnsaar, AnsaarClient, "prices"),
    ):
        assert list(inspect.signature(getattr(fake, name)).parameters) == list(
            inspect.signature(getattr(real, name)).parameters
        )


INDEX_BARS = [bar(THU, 25000.0), bar(FRI, 25100.0), bar(MON, 25200.0), bar(TUE, 25300.0)]


def market(extra=None):
    bars = {"^NSEI": INDEX_BARS, "SHARIABEES.NS": [bar(FRI, 400.0), bar(MON, 402.0)]}
    bars.update(extra or {})
    return FakeFinance(bars)


async def run(pool, ansaar, finance, today):
    return await td.run_tick(pool, ansaar=ansaar, finance=finance, today=today, project=False)


async def open_problems(pool):
    rows = await pool.fetch(
        "SELECT class, subject FROM problems WHERE subject_kind = 'trading_desk' "
        "AND status NOT IN ('resolved', 'closed')"
    )
    return sorted((r["class"], r["subject"]) for r in rows)


async def held_trade(pool, day=THU, symbol="TCS", qty=3, price=3000):
    """A buy that filled on ``day``, as if an earlier run placed it."""
    await pool.execute(
        "INSERT INTO finance.desk_plans (data_date, mode, outcome) VALUES ($1, 'paper', 'orders') "
        "ON CONFLICT DO NOTHING",
        day,
    )
    await pool.execute(
        "INSERT INTO finance.desk_orders (mode, data_date, seq, created_day, symbol, asset_class, side, "
        "qty, ref_price, status, fill_date, fill_price, costs, price_source) "
        "VALUES ('paper', $1, 0, $1, $2, 'equity', 'buy', $3, $4, 'filled', $1, $4, 18, 'yahoo')",
        day, symbol, qty, price,
    )


async def test_day_one_plans_and_day_two_fills(pool):
    finance = market({
        "TCS.NS": [bar(FRI, 3000.0), bar(MON, 3100.0)],
        "GOLDBEES.NS": [bar(FRI, 100.0), bar(MON, 101.0)],
    })
    ansaar = FakeAnsaar({
        FRI: [row("TCS", 0.10), row("GOLDBEES", 0.10, cls="etf", rank=2)],
        MON: [row("TCS", 0.10, day=MON), row("GOLDBEES", 0.10, day=MON, cls="etf", rank=2)],
    })

    out = await run(pool, ansaar, finance, MON)
    assert out["day"] == "2026-09-11" and out["planned"] == "orders" and out["findings"] == []
    orders = await pool.fetch("SELECT symbol, side, qty, status, created_day FROM finance.desk_orders ORDER BY seq")
    assert [(o["symbol"], o["side"], o["qty"], o["status"]) for o in orders] == [
        ("TCS", "buy", 3, "pending"),
        ("GOLDBEES", "buy", 100, "pending"),
    ]
    assert {o["created_day"] for o in orders} == {MON}

    out = await run(pool, ansaar, finance, TUE)
    assert out["filled"] == 2 and out["planned"] == "no_change"
    filled = await pool.fetch(
        "SELECT symbol, fill_date, fill_price, costs, price_source FROM finance.desk_orders ORDER BY seq"
    )
    assert [(f["symbol"], f["fill_date"], float(f["fill_price"]), float(f["costs"]), f["price_source"]) for f in filled] == [
        ("TCS", MON, 3100.0, pytest.approx(18.6), "yahoo"),
        ("GOLDBEES", MON, 101.0, pytest.approx(20.2), "yahoo"),
    ]


async def test_a_second_run_the_same_day_changes_nothing(pool):
    finance = market({"TCS.NS": [bar(FRI, 3000.0)]})
    ansaar = FakeAnsaar({FRI: [row("TCS", 0.10)]})
    await run(pool, ansaar, finance, MON)
    await run(pool, ansaar, finance, MON)
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_orders") == 1
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_plans") == 1


async def test_a_stale_day_holds_raises_one_problem_and_the_next_good_day_clears_it(pool):
    finance = market({"TCS.NS": [bar(FRI, 3000.0), bar(MON, 3000.0)]})
    ansaar = FakeAnsaar({FRI: [], MON: [row("TCS", 0.10, day=MON)]})
    out = await run(pool, ansaar, finance, MON)
    assert out["planned"] == "held_stale"
    assert await open_problems(pool) == [("desk_decisions_stale", "decisions")]
    await run(pool, ansaar, finance, MON)
    assert await pool.fetchval("SELECT count(*) FROM problems WHERE subject_kind = 'trading_desk'") == 1
    assert await open_problems(pool) == [("desk_decisions_stale", "decisions")]
    out = await run(pool, ansaar, finance, TUE)
    assert out["planned"] == "orders"
    assert await open_problems(pool) == []


async def test_ansaar_down_holds_and_says_so_without_a_stale_problem(pool):
    out = await run(pool, FakeAnsaar(fail=True), market(), MON)
    assert out["planned"] == "held_stale"
    assert await open_problems(pool) == [("desk_source_error", "ansaar")]


async def test_a_rerun_after_ansaar_recovers_keeps_the_day_s_problem(pool):
    await run(pool, FakeAnsaar(fail=True), market(), MON)
    await run(pool, FakeAnsaar({FRI: [row("TCS", 0.10)]}), market({"TCS.NS": [bar(FRI, 3000.0)]}), MON)
    assert await open_problems(pool) == [("desk_source_error", "ansaar")]
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_orders") == 0


async def test_yahoo_down_writes_no_plan_and_raises_a_source_error(pool):
    out = await run(pool, FakeAnsaar(), FakeFinance(fail=True), MON)
    assert out["skipped"] == "yahoo"
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_plans") == 0
    assert await open_problems(pool) == [("desk_source_error", "yahoo")]


async def test_a_vanished_holding_class_holds_the_portfolio(pool):
    await held_trade(pool)
    finance = market({"TCS.NS": [bar(THU, 3000.0), bar(FRI, 3000.0)], "GOLDBEES.NS": [bar(FRI, 100.0)]})
    out = await run(pool, FakeAnsaar({FRI: [row("GOLDBEES", 0.10, cls="etf")]}), finance, MON)
    assert out["planned"] == "held_suspect"
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_orders WHERE status = 'pending'") == 0
    assert await open_problems(pool) == [("desk_decisions_suspect", "decisions")]


async def test_a_non_compliant_row_is_dropped_the_rest_trades_and_it_is_reported(pool):
    finance = market({"TCS.NS": [bar(FRI, 3000.0)], "XYZ.NS": [bar(FRI, 50.0)]})
    ansaar = FakeAnsaar({FRI: [row("TCS", 0.10), row("XYZ", 0.10, rank=2, halal="NON_COMPLIANT")]})
    out = await run(pool, ansaar, finance, MON)
    assert out["planned"] == "orders"
    assert [r["symbol"] for r in await pool.fetch("SELECT symbol FROM finance.desk_orders")] == ["TCS"]
    assert await open_problems(pool) == [("desk_decisions_suspect", "decisions")]


async def test_the_copy_keeps_what_was_served_first(pool):
    finance = market({"TCS.NS": [bar(FRI, 3000.0)]})
    await run(pool, FakeAnsaar({FRI: [row("TCS", 0.10)]}), finance, MON)
    await run(pool, FakeAnsaar({FRI: [row("TCS", 0.20)]}), finance, MON)
    weight = await pool.fetchval("SELECT target_weight FROM finance.desk_decisions WHERE data_date = $1", FRI)
    assert float(weight) == 0.10


async def test_a_stored_close_is_never_rewritten(pool):
    finance = market({"TCS.NS": [bar(FRI, 3000.0)]})
    ansaar = FakeAnsaar({FRI: [row("TCS", 0.10)]})
    await run(pool, ansaar, finance, MON)
    finance.bars["TCS.NS"] = [bar(FRI, 1500.0), bar(MON, 1510.0, split=2.0)]
    await run(pool, ansaar, finance, TUE)
    rows = await pool.fetch("SELECT date, close, split_ratio FROM finance.desk_prices WHERE symbol = 'TCS.NS' ORDER BY date")
    assert [(r["date"], float(r["close"]), r["split_ratio"] and float(r["split_ratio"])) for r in rows] == [
        (FRI, 3000.0, None),
        (MON, 1510.0, 2.0),
    ]


async def test_store_bars_never_keeps_today(pool):
    await td._store_bars(pool, "TCS.NS", [bar(FRI, 3000.0), bar(MON, 3010.0)], "yahoo", MON)
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_prices WHERE symbol = 'TCS.NS'") == 1


async def test_a_symbol_yahoo_lacks_is_priced_from_ansaar_and_marked(pool):
    ansaar = FakeAnsaar(
        {FRI: [row("GOLDBEES", 0.10, cls="etf")], MON: [row("GOLDBEES", 0.10, day=MON, cls="etf")]},
        prices={"GOLDBEES": [bar(FRI, 100.0), bar(MON, 101.0)]},
    )
    await run(pool, ansaar, market(), MON)
    await run(pool, ansaar, market(), TUE)
    assert await pool.fetchval("SELECT price_source FROM finance.desk_orders WHERE symbol = 'GOLDBEES'") == "ansaar"


async def test_a_holding_with_no_recent_price_raises_price_missing(pool):
    await held_trade(pool)
    await pool.execute("INSERT INTO finance.desk_prices (symbol, date, close, source) VALUES ('TCS.NS', $1, 3000, 'yahoo')", THU)
    await run(pool, FakeAnsaar({TUE: [row("TCS", 0.10, day=TUE)]}), market(), WED)
    assert ("desk_price_missing", "tcs") in await open_problems(pool)


async def test_live_mode_is_refused(pool):
    await pool.execute(
        "INSERT INTO activities (slug, workflow_type, agent_id, schedule_cron, config, active) "
        "VALUES ($1, 'TradingDeskFlow', 'maou', '30 2 * * 1-5', $2, false) "
        "ON CONFLICT (slug) DO UPDATE SET config = EXCLUDED.config",
        td.DESK_SLUG,
        {"mode": "live"},
    )
    out = await run(pool, FakeAnsaar(), market(), MON)
    assert out["skipped"] == "mode"
    assert await open_problems(pool) == [("desk_source_error", "config")]


def test_yahoo_symbol():
    assert td.yahoo_symbol("TCS") == "TCS.NS"
    assert td.yahoo_symbol("^NSEI") == "^NSEI"
    assert td.yahoo_symbol("SHARIABEES.NS") == "SHARIABEES.NS"
```

- [ ] **Step 3: Run the tests; they must fail with `ModuleNotFoundError: aegis.services.trading_desk`.**
  Use `<PATHS>` = `tests/core/services/test_trading_desk.py`, `<NAME>` = `desk-t6`. The test
  database is built from `migrations/`, so migration 045 applies automatically.

- [ ] **Step 4: Write `core/src/aegis/services/trading_desk.py`**

```python
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
PRICE_GRACE_DAYS = 3


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


def _f(raw: Any) -> float | None:
    return float(raw) if raw is not None else None


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
        (symbol, b["day"], b.get("close"), b.get("split_ratio"), b.get("dividend"), source)
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


async def _refresh(
    pool: asyncpg.Pool,
    finance: Any,
    ansaar: Any,
    symbol: str,
    asset_class: str | None,
    today: date,
    *,
    required: bool = False,
) -> None:
    """Fetch and store ``symbol``'s recent bars: Yahoo first, then ansaar when
    Yahoo has none. Raises only when ``required`` and Yahoo fails."""
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
        return
    if ansaar is None or asset_class is None:
        return
    try:
        bars = await ansaar.prices(symbol, asset_class, start, end)
    except AnsaarError as exc:
        logger.warning("trading_desk_ansaar_prices_failed", symbol=symbol, error=str(exc)[:200])
        return
    await _store_bars(pool, ysym, bars, "ansaar", today)


async def _bars(pool: asyncpg.Pool, symbols: set[str]) -> dict[str, list[dm.Bar]]:
    """Stored bars keyed by the desk's symbol: NSE form for instruments, and the
    benchmarks as named. Each list is sorted by day."""
    by_yahoo = {yahoo_symbol(s): s for s in symbols}
    rows = await pool.fetch(
        "SELECT symbol, date, close, split_ratio, dividend, source FROM finance.desk_prices "
        "WHERE symbol = ANY($1::text[]) ORDER BY symbol, date",
        list(by_yahoo),
    )
    out: dict[str, list[dm.Bar]] = {s: [] for s in symbols}
    for r in rows:
        out[by_yahoo[r["symbol"]]].append(
            dm.Bar(r["date"], _f(r["close"]), _f(r["split_ratio"]), _f(r["dividend"]), r["source"])
        )
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
                r.order_id, r.fill_day, r.price, r.qty, r.costs, r.source,
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
) -> None:
    """The plan row and its orders in one transaction, so a date is acted on once."""
    async with pool.acquire() as conn, conn.transaction():
        inserted = await conn.fetchval(
            "INSERT INTO finance.desk_plans (data_date, mode, outcome, findings, skipped) "
            "VALUES ($1, 'paper', $2, $3, $4) ON CONFLICT (data_date) DO NOTHING RETURNING data_date",
            day, outcome, findings, skipped,
        )
        if inserted is None:
            return  # another run planned this date first
        await conn.executemany(
            "INSERT INTO finance.desk_orders (mode, data_date, seq, created_day, symbol, asset_class, "
            "side, qty, ref_price) VALUES ('paper', $1, $2, $3, $4, $5, $6, $7, $8)",
            [(day, i, created_day, o.symbol, o.asset_class, o.side, o.qty, o.ref_price) for i, o in enumerate(orders)],
        )


# --- the run -----------------------------------------------------------------


async def run_tick(
    pool: asyncpg.Pool, *, ansaar: Any, finance: Any, today: date | None = None, project: bool = True
) -> dict:
    """One morning's run (spec §3). Idempotent: a second run on the same day
    changes nothing and raises the same problems."""
    today = today or datetime.now(MARKET_TZ).date()
    out, findings = await _tick(pool, ansaar, finance, today)
    unique = list({(f["klass"], f["subject"]): f for f in findings}.values())
    await hub_watch.reconcile_findings(
        pool, source=SOURCE, subject_kind=SUBJECT_KIND, classes=DAILY_CLASSES, findings=unique, project=project
    )
    out["findings"] = sorted(f["klass"] for f in unique)
    return out


async def _tick(pool: asyncpg.Pool, ansaar: Any, finance: Any, today: date) -> tuple[dict, list[dict]]:
    rules = await load_rules(pool)
    out: dict[str, Any] = {"today": today.isoformat()}
    if rules.mode != "paper":
        return out | {"skipped": "mode"}, [
            _finding(
                "desk_source_error", "config", f"Trading desk: mode '{rules.mode}' is not built",
                "Only paper mode exists. Set mode back to paper in the trading-desk-daily config. "
                "Nothing was traded.",
            )
        ]

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
        ]
    index_days = [b.day for b in (await _bars(pool, {INDEX}))[INDEX] if b.close is not None]
    day = dm.last_trading_day(index_days, today)
    if day is None:
        return out | {"skipped": "no_market_day"}, [
            _finding(
                "desk_source_error", "yahoo", "Trading desk: no market days from Yahoo",
                f"Yahoo returned no {INDEX} bars before {today}. Nothing was traded.",
            )
        ]
    out["day"] = day.isoformat()

    # 2. Copy.
    findings: list[dict] = []
    ansaar_failure: dict | None = None
    try:
        rows, _meta = await ansaar.decisions(day)
        await _store_decisions(pool, day, [r for r in rows if str(r.get("data_date") or "")[:10] == day.isoformat()])
    except AnsaarError as exc:
        ansaar_failure = _finding(
            "desk_source_error", "ansaar", "Trading desk: can't reach ansaar",
            f"Fetching the decisions for {day} failed ({exc}). The desk held its positions and "
            "traded nothing that day.",
        )
        findings.append(ansaar_failure)
    decisions = await _decisions(pool, day)

    # 3. Prices: held names, decided names, pending orders and both benchmarks.
    fills = await _fills(pool)
    pending = await _pending(pool)
    net: dict[str, float] = defaultdict(float)  # ignores splits: it only chooses what to fetch
    classes: dict[str, str] = {}
    for f in fills:
        net[f.symbol] += f.qty if f.side == "buy" else -f.qty
        classes[f.symbol] = f.asset_class
    wanted = {s: classes[s] for s, q in net.items() if q > 0}
    wanted |= {d.symbol: d.asset_class for d in decisions if d.asset_class in rules.asset_classes}
    wanted |= {o.symbol: o.asset_class for o in pending}
    for symbol, asset_class in sorted(wanted.items()):
        await _refresh(pool, finance, ansaar, symbol, asset_class, today)
    for bench in {rules.benchmark, rules.context_benchmark} - {INDEX}:
        await _refresh(pool, finance, None, bench, None, today)
    bars = await _bars(pool, set(wanted))

    # 4. Fill.
    if pending:
        results = dm.fill_orders(pending, bars, index_days, dm.replay(fills, bars, rules.capital, today), rules)
        await _apply_fills(pool, results)
        out["filled"] = sum(r.status == "filled" for r in results)
        fills = await _fills(pool)

    # 5. Plan, once per date and never while an order is pending.
    planned = await pool.fetchval("SELECT 1 FROM finance.desk_plans WHERE data_date = $1", day)
    still_pending = await pool.fetchval("SELECT 1 FROM finance.desk_orders WHERE status = 'pending' LIMIT 1")
    if not planned and not still_pending:
        book = dm.replay(fills, bars, rules.capital, day)
        check = dm.check_decisions(decisions, book.held_classes(), rules)
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
        if check.outcome == "ok":
            symbols = {d.symbol for d in check.rows} | set(book.held())
            closes = {s: px for s in symbols if (px := dm.close_on(bars.get(s, []), day)) is not None}
            orders, skipped = dm.plan_orders(check.rows, book, closes, rules)
        outcome = check.outcome if check.outcome != "ok" else ("orders" if orders else "no_change")
        await _write_plan(pool, day, outcome, plan_findings, skipped, orders, today)
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
    return out, findings
```

- [ ] **Step 5: Run the tests; all must pass.** If `test_a_rerun_after_ansaar_recovers_keeps_the_day_s_problem`
  fails, the plan's stored findings are not being re-raised. Fix that; do not weaken the test.

- [ ] **Step 6: Prove the tests can fail.**
  1. Replace the `findings += list(await pool.fetchval(...))` line with
     `findings += plan_findings if not planned and not still_pending else []`.
     `test_a_stale_day_holds_raises_one_problem_and_the_next_good_day_clears_it` must fail on its
     second run. Restore it.
  2. Change the price insert's `COALESCE(finance.desk_prices.close, EXCLUDED.close)` to
     `EXCLUDED.close`. `test_a_stored_close_is_never_rewritten` must fail. Restore it.

- [ ] **Step 7: Run the whole core suite once** (`<PATHS>` = `tests/core/`, `<NAME>` = `desk-t6-core`).
  The migration must not break anything else. Then lint and commit:

```bash
git add migrations/045_trading_desk.sql core/src/aegis/services/trading_desk.py tests/core/services/test_trading_desk.py
git commit -m "feat(desk): the trading desk's tables and its daily run"
```

---

### Task 7: The monthly summary, its check, and the month-close section

**Files:**
- Modify: `core/src/aegis/services/trading_desk.py` (append)
- Modify: `worker/src/aegis_worker/activities/money.py`: the import on line 17, and the lines
  just before `return close` at the end of `build_month_close`
- Modify: `worker/src/aegis_worker/activities/money_render.py`: add `desk_lines`, and call it at
  the end of `render_month_close`
- Test: `tests/core/services/test_trading_desk_summary.py`, `tests/worker/test_money_render.py` (append)

**Interfaces:**
- Consumes: Task 6's `_fills`, `_bars`, `load_rules`, `_finding` and the constants; Task 4's scoring.
- Produces:
  - `month_summary(pool, month_first: date, next_first: date) -> dict | None`. Its keys are
    `since, weeks, capital, value, after_tax, benchmark, benchmark_value, context, context_value,
    mean_gap, t, label, below_expectation, expected_excess_pa, holdings, cash_pct, filled, costs,
    cancelled, held_back, ansaar_prices, moves`.
  - `reconcile_expectation(pool, summary: dict | None, *, project: bool = True) -> None`
  - `desk_lines(desk: dict) -> list[str]` in `money_render`.

- [ ] **Step 1: Write the failing tests**

`tests/core/services/test_trading_desk_summary.py`:

```python
"""The trading desk's monthly section and its check (spec §8, §9)."""

from __future__ import annotations

from datetime import date

import pytest
import pytest_asyncio

from aegis.services import trading_desk as td

SEP, OCT = date(2026, 9, 1), date(2026, 10, 1)
_WIPE = (
    "DELETE FROM finance.desk_orders",
    "DELETE FROM finance.desk_plans",
    "DELETE FROM finance.desk_decisions",
    "DELETE FROM finance.desk_prices",
    "DELETE FROM problem_events WHERE problem_id IN (SELECT id FROM problems WHERE subject_kind = 'trading_desk')",
    "DELETE FROM problems WHERE subject_kind = 'trading_desk'",
)


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    for sql in _WIPE:
        await db_pool.execute(sql)
    yield db_pool
    for sql in _WIPE:
        await db_pool.execute(sql)


async def price(pool, symbol, day, close):
    await pool.execute(
        "INSERT INTO finance.desk_prices (symbol, date, close, source) VALUES ($1, $2, $3, 'yahoo')",
        symbol, day, close,
    )


async def fill(pool, day, side, qty, px, costs, seq=0):
    await pool.execute(
        "INSERT INTO finance.desk_plans (data_date, mode, outcome) VALUES ($1, 'paper', 'orders') ON CONFLICT DO NOTHING",
        day,
    )
    await pool.execute(
        "INSERT INTO finance.desk_orders (mode, data_date, seq, created_day, symbol, asset_class, side, qty, "
        "ref_price, status, fill_date, fill_price, costs, price_source) "
        "VALUES ('paper', $1, $2, $1, 'TCS', 'equity', $3, $4, $5, 'filled', $1, $5, $6, 'yahoo')",
        day, seq, side, qty, px, costs,
    )


async def september(pool):
    for day in (14, 18, 25, 30):
        await price(pool, "^NSEI", date(2026, 9, day), 25000.0 if day == 14 else 25250.0)
    await price(pool, "SHARIABEES.NS", date(2026, 9, 14), 400.0)
    await price(pool, "SHARIABEES.NS", date(2026, 9, 30), 404.0)
    await price(pool, "TCS.NS", date(2026, 9, 14), 1000.0)
    await price(pool, "TCS.NS", date(2026, 9, 30), 1100.0)
    await fill(pool, date(2026, 9, 14), "buy", 10, 1000.0, 20.0)


async def test_no_section_before_the_first_fill(pool):
    assert await td.month_summary(pool, SEP, OCT) is None


async def test_the_month_s_value_benchmarks_and_holdings(pool):
    await september(pool)
    s = await td.month_summary(pool, SEP, OCT)
    assert s["since"] == "2026-09-14"
    assert s["value"] == pytest.approx(100_980.0)
    assert s["after_tax"] == pytest.approx(100_980.0)
    assert s["benchmark_value"] == pytest.approx(100_000 * 0.998 * 404 / 400)
    assert s["context_value"] == pytest.approx(101_000.0)
    assert s["holdings"] == ["TCS"]
    assert s["cash_pct"] == pytest.approx(89_980 / 100_980)
    assert (s["filled"], s["costs"]) == (1, 20.0)
    assert s["label"] == "too early" and s["below_expectation"] is False


async def test_after_tax_takes_off_the_year_s_short_term_tax(pool):
    await september(pool)
    await fill(pool, date(2026, 9, 30), "sell", 10, 1100.0, 38.0)
    s = await td.month_summary(pool, SEP, OCT)
    assert s["value"] == pytest.approx(100_942.0)
    assert s["after_tax"] == pytest.approx(100_942.0 - 0.20 * 942.0)
    assert s["holdings"] == []


async def test_held_back_days_are_counted(pool):
    await september(pool)
    await pool.execute(
        "INSERT INTO finance.desk_plans (data_date, mode, outcome) VALUES ($1, 'paper', 'held_stale')",
        date(2026, 9, 21),
    )
    assert (await td.month_summary(pool, SEP, OCT))["held_back"] == {"held_stale": 1}


async def open_classes(pool):
    rows = await pool.fetch(
        "SELECT class FROM problems WHERE subject_kind = 'trading_desk' AND status NOT IN ('resolved', 'closed')"
    )
    return sorted(r["class"] for r in rows)


SUMMARY = {"weeks": 20, "benchmark": "SHARIABEES.NS", "mean_gap": -0.003, "t": -1.2, "expected_excess_pa": 0.06}


async def test_below_expectation_raises_then_clears(pool):
    await td.reconcile_expectation(pool, SUMMARY | {"below_expectation": True}, project=False)
    assert await open_classes(pool) == ["desk_below_expectation"]
    await td.reconcile_expectation(pool, SUMMARY | {"below_expectation": False}, project=False)
    assert await open_classes(pool) == []


async def test_the_daily_run_never_resolves_the_monthly_problem(pool):
    await td.reconcile_expectation(pool, SUMMARY | {"below_expectation": True}, project=False)

    class YahooDown:
        async def daily_bars(self, symbol, start, end):
            raise RuntimeError("down")

    await td.run_tick(pool, ansaar=None, finance=YahooDown(), today=date(2026, 9, 14), project=False)
    assert "desk_below_expectation" in await open_classes(pool)
```

Append to `tests/worker/test_money_render.py`:

```python
_DESK = {
    "since": "2026-09-15", "weeks": 14, "capital": 100000.0, "value": 104230.0, "after_tax": 103410.0,
    "benchmark": "SHARIABEES.NS", "benchmark_value": 102100.0, "context": "^NSEI", "context_value": 101300.0,
    "mean_gap": 0.0015, "t": 0.8, "label": "no evidence yet", "below_expectation": False,
    "expected_excess_pa": 0.06, "holdings": ["TCS", "INFY"], "cash_pct": 0.12, "filled": 23, "costs": 612.0,
    "cancelled": {"no_cash": 1}, "held_back": {"held_stale": 1, "held_suspect": 1}, "ansaar_prices": 1,
    "moves": [{"symbol": "XYZ", "day": "2026-11-03", "move": -0.51}],
}


def test_month_close_renders_the_desk_section():
    out = render_month_close({"month": "2026-09", "books_ok": False, "desk": _DESK})
    assert "<b>Trading desk (paper)</b>" in out["html"]
    md = out["markdown"]
    assert "## Trading desk (paper)" in md
    assert "14 weeks since 2026-09-15" in md
    assert "t = 0.8: no evidence yet" in md
    assert "Holding 2 names, 12% cash: TCS, INFY" in md
    assert "Days held back: 2 (1 stale, 1 suspect)." in md
    assert "Cancelled: 1 no_cash." in md
    assert "Check: XYZ moved -51% on 2026-11-03. Possible bad price or missing split." in md


def test_month_close_without_a_desk_has_no_desk_section():
    out = render_month_close({"month": "2026-09", "books_ok": False})
    assert "Trading desk" not in out["markdown"] and "Trading desk" not in out["html"]
```

If `render_month_close` is not already imported at the top of `tests/worker/test_money_render.py`,
add `from aegis_worker.activities.money_render import render_month_close`.

- [ ] **Step 2: Run both files; they must fail.** `month_summary` doesn't exist yet
  (`AttributeError`), and the render has no desk section. Use `<PATHS>` =
  `tests/core/services/test_trading_desk_summary.py tests/worker/test_money_render.py`,
  `<NAME>` = `desk-t7`.

- [ ] **Step 3a: Append to `core/src/aegis/services/trading_desk.py`**

```python
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
    desk = dm.desk_values(fills, bars, rules.capital, days)
    bench = dm.benchmark_values(bars[rules.benchmark], rules.capital, rules.cost_pct_per_side, days)
    context = dm.benchmark_values(bars[rules.context_benchmark], rules.capital, 0.0, days)
    book = dm.replay(fills, bars, rules.capital, month_end)
    end_value = desk[-1][1]
    st = dm.stats(dm.weekly_excess(desk, bench))
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
        "below_expectation": dm.below_expectation(st, rules.expected_excess_pa),
        "expected_excess_pa": rules.expected_excess_pa,
        "holdings": sorted(held, key=lambda s: -worth[s]),
        "cash_pct": book.cash / end_value if end_value else 0.0,
        "filled": sum(r["status"] == "filled" for r in orders),
        "costs": round(sum(float(r["costs"] or 0) for r in orders if r["status"] == "filled"), 2),
        "cancelled": dict(cancelled),
        "held_back": {r["outcome"]: r["n"] for r in held_back},
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
                f"Over {summary['weeks']} weeks the desk's weekly gap to {summary['benchmark']} averaged "
                f"{summary['mean_gap']:+.2%} (t = {summary['t']:.1f}). That is more than two standard "
                f"errors below the {summary['expected_excess_pa']:.0%} a year the backtest implies. Look "
                "at the trading system before trusting it with money. This comes back each month while "
                "it stays true.",
            )
        )
    await hub_watch.reconcile_findings(
        pool, source=SOURCE, subject_kind=SUBJECT_KIND, classes=MONTHLY_CLASSES, findings=findings, project=project
    )
```

- [ ] **Step 3b: Wire the section into the month close.** In `worker/src/aegis_worker/activities/money.py`:
  - Change line 17 from `from aegis.services import books, ledger_write, reconciled` to
    `from aegis.services import books, ledger_write, reconciled, trading_desk`.
  - Insert these lines immediately before the final `return close` of `build_month_close`
    (around line 1231). `month_first` and `this_first` are already local there.

```python
        # The trading desk's section and its monthly check (desk spec §8-§9).
        # A desk failure must never break the close.
        try:
            close["desk"] = await trading_desk.month_summary(self.db_pool, month_first, this_first)
            await trading_desk.reconcile_expectation(self.db_pool, close["desk"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("month_close_desk_failed", error=str(exc)[:200])
            close["desk"] = None
```

- [ ] **Step 3c: Render it.** In `worker/src/aegis_worker/activities/money_render.py`, add
  above `render_month_close`:

```python
def desk_lines(desk: dict) -> list[str]:
    """The trading desk's month-close section (desk spec §9), one line each."""
    lines = [
        f"{desk['weeks']} weeks since {desk['since']}. Capital {_money(desk['capital'])}.",
        f"Value {_money(desk['value'])} (after tax {_money(desk['after_tax'])}) · "
        f"{desk['benchmark']} {_money(desk['benchmark_value'])} · {desk['context']} {_money(desk['context_value'])}",
        f"Weekly gap to {desk['benchmark']}: {desk['mean_gap']:+.2%} on average, "
        f"t = {desk['t']:.1f}: {desk['label']}",
        f"Holding {len(desk['holdings'])} names, {desk['cash_pct']:.0%} cash"
        + (f": {', '.join(desk['holdings'][:12])}" if desk["holdings"] else "."),
        f"This month: {desk['filled']} orders filled, {_money(desk['costs'])} in costs.",
    ]
    if desk["cancelled"]:
        lines.append("Cancelled: " + ", ".join(f"{n} {why}" for why, n in sorted(desk["cancelled"].items())) + ".")
    held_back = desk["held_back"]
    if held_back:
        lines.append(
            f"Days held back: {sum(held_back.values())} "
            f"({held_back.get('held_stale', 0)} stale, {held_back.get('held_suspect', 0)} suspect)."
        )
    if desk["ansaar_prices"]:
        lines.append(f"Prices from ansaar: {desk['ansaar_prices']}.")
    lines += [
        f"Check: {m['symbol']} moved {m['move']:+.0%} on {m['day']}. Possible bad price or missing split."
        for m in desk["moves"]
    ]
    return lines
```

Then in `render_month_close`, insert before the final `return`:

```python
    desk = close.get("desk")
    if desk:
        dl = desk_lines(desk)
        html += ["", "<b>Trading desk (paper)</b>", *(escape(ln) for ln in dl)]
        md += ["", "## Trading desk (paper)", "", *(f"- {ln}" for ln in dl)]
```

- [ ] **Step 4: Run both test files, plus `tests/worker/activities/test_money_report_activities.py`;
  all must pass.** The existing close tests prove the new block leaves the rest of the close as it
  was.

- [ ] **Step 5: Prove the tests can fail.** Delete the `reconcile_expectation` call from the
  daily path. It isn't there, so instead confirm the separation: temporarily add
  `"desk_below_expectation"` to `DAILY_CLASSES`.
  `test_the_daily_run_never_resolves_the_monthly_problem` must fail. Restore it.

- [ ] **Step 6: Lint and commit**

```bash
git add core/src/aegis/services/trading_desk.py worker/src/aegis_worker/activities/money.py worker/src/aegis_worker/activities/money_render.py tests/core/services/test_trading_desk_summary.py tests/worker/test_money_render.py
git commit -m "feat(desk): a trading desk section in the month close, with its monthly check"
```

---

### Task 8: Settings, the activity, the flow, registration and docs

**Files:**
- Modify: `core/src/aegis/config.py`: two fields after `books_todoist_projects`
- Modify: `core/src/aegis/services/integrations_config.py`: two `ConfigKey`s at the end of
  `CONFIG_REGISTRY`, and `read_integration`
- Create: `worker/src/aegis_worker/activities/trading_desk.py`
- Create: `worker/src/aegis_worker/flows/trading_desk.py`
- Modify: `worker/src/aegis_worker/registry.py`: the import, the `FlowSpec` after
  `StatementReconcileFlow`, and the `ACTIVITY_CLASS_FLAGS` entry
- Modify: `worker/src/aegis_worker/__main__.py`: construct after `statement_act`, and add to
  `collect_activities(...)`
- Modify: `config/seed/activities.yaml`: a row after `money-statements-reconcile`
- Modify: `CLAUDE.md`: one paragraph
- Test:
  - `tests/core/test_integrations_read.py` (create)
  - `tests/worker/activities/test_trading_desk_activity.py` (create)
  - `tests/worker/flows/test_trading_desk_flow.py` (create)
  - `tests/worker/test_schedule_sync_mappers.py` (append)
  - `tests/worker/test_registry.py` (update counts)

**Interfaces:**
- Consumes: `trading_desk.run_tick` (Task 6), `AnsaarClient` and `FinanceConnector` (Task 5),
  and `integrations_config.save_integration`, which already exists.
- Produces:
  - `read_integration(pool, settings, key) -> str`
  - `TradingDeskActivities(*, db_pool, settings, finance=None)` with the activity `desk_tick() -> dict`
  - `TradingDeskFlow` and `TradingDeskConfig(agent_id: str = "maou")`

- [ ] **Step 1: Write the failing tests**

`tests/core/test_integrations_read.py`:

```python
"""Reading one integration value now, DB first (trading desk spec §12)."""

from __future__ import annotations

import pytest_asyncio

from aegis.services.integrations_config import read_integration, save_integration

_KEYS = ("integration:ansaar_url", "integration:ansaar_service_secret")


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key = ANY($1::text[])", list(_KEYS))
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = ANY($1::text[])", list(_KEYS))


async def test_the_db_row_wins_and_a_secret_is_decrypted(pool, test_settings):
    test_settings.ansaar_url = "http://env.example"
    await save_integration(pool, test_settings, "ansaar_url", "http://ansaar-data:3000")
    await save_integration(pool, test_settings, "ansaar_service_secret", "s3cret")
    assert await read_integration(pool, test_settings, "ansaar_url") == "http://ansaar-data:3000"
    assert await read_integration(pool, test_settings, "ansaar_service_secret") == "s3cret"


async def test_no_db_row_falls_back_to_settings_then_empty(pool, test_settings):
    test_settings.ansaar_url = "http://env.example"
    assert await read_integration(pool, test_settings, "ansaar_url") == "http://env.example"
    assert await read_integration(pool, test_settings, "ansaar_service_secret") == ""
```

`tests/worker/activities/test_trading_desk_activity.py`:

```python
"""TradingDeskActivities.desk_tick: the wiring between settings and the run."""

from __future__ import annotations

import pytest_asyncio
from temporalio.testing import ActivityEnvironment

from aegis.connectors.finance import FinanceConnector
from aegis.services import trading_desk
from aegis.services.integrations_config import save_integration
from aegis_worker.activities.trading_desk import TradingDeskActivities

_KEYS = ["integration:ansaar_url", "integration:ansaar_service_secret"]


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key = ANY($1::text[])", _KEYS)
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = ANY($1::text[])", _KEYS)


async def test_an_unconfigured_desk_does_nothing(pool, test_settings, monkeypatch):
    called = []
    monkeypatch.setattr(trading_desk, "run_tick", lambda *a, **k: called.append(1))
    act = TradingDeskActivities(db_pool=pool, settings=test_settings)
    assert await ActivityEnvironment().run(act.desk_tick) == {"skipped": "unconfigured"}
    assert called == []


async def test_a_configured_desk_runs_with_the_saved_connection(pool, test_settings, monkeypatch):
    await save_integration(pool, test_settings, "ansaar_url", "http://ansaar.test")
    await save_integration(pool, test_settings, "ansaar_service_secret", "s3cret")
    seen: dict = {}

    async def fake_run_tick(p, *, ansaar, finance, **kw):
        seen.update(url=ansaar._url, secret=ansaar._secret, finance=finance)
        return {"day": "2026-09-11"}

    monkeypatch.setattr(trading_desk, "run_tick", fake_run_tick)
    act = TradingDeskActivities(db_pool=pool, settings=test_settings)
    assert await ActivityEnvironment().run(act.desk_tick) == {"day": "2026-09-11"}
    assert (seen["url"], seen["secret"]) == ("http://ansaar.test", "s3cret")
    assert isinstance(seen["finance"], FinanceConnector)
```

`tests/worker/flows/test_trading_desk_flow.py`:

```python
"""TradingDeskFlow runs exactly one desk_tick and returns what it said."""

from __future__ import annotations

import uuid

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from aegis_worker.flows.trading_desk import TradingDeskConfig, TradingDeskFlow


async def test_the_flow_runs_one_desk_tick():
    calls: list[int] = []

    @activity.defn(name="desk_tick")
    async def desk_tick() -> dict:
        calls.append(1)
        return {"day": "2026-09-11", "planned": "orders"}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4()}"
        async with Worker(env.client, task_queue=queue, workflows=[TradingDeskFlow], activities=[desk_tick]):
            out = await env.client.execute_workflow(
                TradingDeskFlow.run, TradingDeskConfig(agent_id="maou"), id=f"desk-{uuid.uuid4()}", task_queue=queue
            )
    assert out == {"day": "2026-09-11", "planned": "orders"}
    assert calls == [1]
```

Append to `tests/worker/test_schedule_sync_mappers.py`, importing `TradingDeskConfig` and
`TradingDeskFlow` from `aegis_worker.flows.trading_desk` at the top:

```python
def test_trading_desk_flow_mapper_resolves():
    mapper = _ACTIVITY_TYPE_MAP["TradingDeskFlow"]
    workflow_cls, cfg = mapper(_act("trading-desk-daily", "TradingDeskFlow", {"mode": "paper"}))
    assert workflow_cls is TradingDeskFlow
    assert isinstance(cfg, TradingDeskConfig)
    assert cfg.agent_id == "maou"
```

- [ ] **Step 2: Run them; they must fail on the missing imports.** Use `<PATHS>` =
  `tests/core/test_integrations_read.py tests/worker/activities/test_trading_desk_activity.py tests/worker/flows/test_trading_desk_flow.py tests/worker/test_schedule_sync_mappers.py`,
  `<NAME>` = `desk-t8`.

- [ ] **Step 3a: Settings.** In `core/src/aegis/config.py`, after the `books_todoist_projects` line:

```python
    # Maou's trading desk (spec 2026-09-12-maou-trading-desk-design.md): the
    # ansaar-data API serving the trading system's decisions. Either empty ⇒ off.
    ansaar_url: str = ""
    ansaar_service_secret: str = ""
```

- [ ] **Step 3b: Admin keys and `read_integration`.** In `core/src/aegis/services/integrations_config.py`,
  append to `CONFIG_REGISTRY`, after the `books_todoist_projects` entry and before the closing `]`:

```python
    ConfigKey(
        "ansaar_url", "ansaar-data URL", "Trading desk", False,
        help="Where Maou's trading desk reads the trading system's decisions, e.g. "
        "http://ansaar-data:3000 on the swarm overlay. Empty = the desk does nothing. "
        "Applies on the next run.",
    ),
    ConfigKey(
        "ansaar_service_secret", "Client-token service secret", "Trading desk", True,
        help="ansaar-data's CLIENT_TOKEN_SECRET, exchanged for a 15-minute token on each run. "
        "Never the admin login.",
    ),
```

and add after `_skey`:

```python
async def read_integration(pool: Any, settings: Any, key: str) -> str:
    """One integration value, read now: the DB row first (decrypted when it is a
    secret), then the Settings field. For a caller that must see an admin save
    without a restart, since the worker applies the overlay only at boot. Never raises."""
    spec = _BY_KEY[key]
    try:
        stored = await pool.fetchval("SELECT value FROM settings WHERE key = $1", _skey(key))
    except Exception as exc:  # noqa: BLE001 — a config read must never break a run
        logger.warning("integration_read_failed", key=key, error=str(exc)[:200])
        stored = None
    if isinstance(stored, dict):
        val = _resolve(spec, stored, getattr(settings, "secret_key", ""))
        if val:
            return val
    return str(getattr(settings, key, "") or "")
```

Check that `_skey(field)` returns `_PREFIX + field`. If it returns anything else, use
`_PREFIX + key`.

- [ ] **Step 3c: The activity** (`worker/src/aegis_worker/activities/trading_desk.py`)

```python
"""TradingDeskActivities: Maou's paper trading desk (spec 2026-09-12-maou-trading-desk-design.md).

One activity. The connection is read from the DB on every run, so an admin save
applies without a worker restart. The run itself is `trading_desk.run_tick`.
"""

from __future__ import annotations

from typing import Any

import structlog
from temporalio import activity

from aegis.connectors.ansaar import AnsaarClient
from aegis.connectors.finance import FinanceConnector
from aegis.services import integrations_config, trading_desk

logger = structlog.get_logger()


class TradingDeskActivities:
    def __init__(self, *, db_pool: Any, settings: Any, finance: FinanceConnector | None = None) -> None:
        self.db_pool = db_pool
        self.settings = settings
        self.finance = finance or FinanceConnector(db_pool=db_pool)

    @activity.defn
    async def desk_tick(self) -> dict:
        url = await integrations_config.read_integration(self.db_pool, self.settings, "ansaar_url")
        secret = await integrations_config.read_integration(self.db_pool, self.settings, "ansaar_service_secret")
        if not url or not secret:
            logger.info("trading_desk_unconfigured")
            return {"skipped": "unconfigured"}
        ansaar = AnsaarClient(url, secret, db_pool=self.db_pool)
        try:
            return await trading_desk.run_tick(self.db_pool, ansaar=ansaar, finance=self.finance)
        finally:
            await ansaar.close()
```

- [ ] **Step 3d: The flow** (`worker/src/aegis_worker/flows/trading_desk.py`)

```python
"""TradingDeskFlow: one weekday-morning run of Maou's paper trading desk.

Everything happens in the `desk_tick` activity, which is idempotent, so a retry
or a manual re-run on the same day changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.shared.retry import FAST


@dataclass
class TradingDeskConfig:
    agent_id: str = "maou"


@workflow.defn(name="TradingDeskFlow")
class TradingDeskFlow:
    @workflow.run
    async def run(self, config: TradingDeskConfig) -> dict:
        return await workflow.execute_activity(
            "desk_tick", start_to_close_timeout=timedelta(minutes=5), retry_policy=FAST
        )
```

- [ ] **Step 3e: Register.** In `worker/src/aegis_worker/registry.py`:
  - Add `from aegis_worker.flows.trading_desk import TradingDeskConfig, TradingDeskFlow` in
    alphabetical order, after the `todoist_sync` import.
  - After the `StatementReconcileFlow` `FlowSpec(...)` entry, add:

```python
    # Maou's paper trading desk (spec 2026-09-12). Its rules are read from the
    # activities row by the activity itself, so only agent_id travels here.
    FlowSpec(
        TradingDeskFlow,
        lambda act: TradingDeskConfig(agent_id=act["agent_id"]),
        feature_flag="money_hygiene_enabled",
    ),
```

  - In `ACTIVITY_CLASS_FLAGS`, add `"TradingDeskActivities": "money_hygiene_enabled",`.

In `worker/src/aegis_worker/__main__.py`:
  - Import `TradingDeskActivities` beside the other activity imports.
  - After the `statement_act` block, add:

```python
    # Maou's paper trading desk rides the money flag, like the rest of the lane.
    desk_act = None
    if settings.money_hygiene_enabled:
        desk_act = TradingDeskActivities(
            db_pool=deps.pool, settings=settings, finance=connectors.get("finance")
        )
```

  - Add `desk_act,` to the `collect_activities(...)` arguments, right after `statement_act,`.

- [ ] **Step 3f: Seed row.** In `config/seed/activities.yaml`, after the `money-statements-reconcile` row:

```yaml
  # Maou's paper trading desk (spec 2026-09-12-maou-trading-desk-design.md).
  # Weekdays 02:30 UTC = 08:00 IST, before the 09:15 open. Ships INACTIVE: it
  # needs ansaar_url + ansaar_service_secret (admin Integrations page) and a
  # trading system serving decisions (ansaar-data #30). The activity reads this
  # config on every run, so an edit here needs no redeploy.
  - slug: trading-desk-daily
    workflow_type: TradingDeskFlow
    agent_id: maou
    schedule_cron: "30 2 * * 1-5"
    config:
      mode: paper
      capital: 100000
      asset_classes: [equity, etf]
      cost_pct_per_side: 0.002
      sell_charge_inr: 16
      band_abs: 0.02
      band_rel: 0.25
      max_order_pct: 0.25
      tax_rate: {equity: 0.20, etf: 0.30}
      ltcg_rate: 0.125
      ltcg_exemption_inr: 125000
      benchmark: SHARIABEES.NS
      context_benchmark: ^NSEI
      expected_excess_pa: 0.06
    active: false
```

- [ ] **Step 3g: Registry counts.** In `tests/worker/test_registry.py`, find the parametrised
  `(homelab, money, flows, activities)` rows. Add one flow and one activity to the row where
  `money` is `True`, and leave the two money-off rows alone. On 2026-09-12 that row was
  `(True, True, 45, 226)`, becoming `(True, True, 46, 227)`. If main has moved, add +1/+1 to
  whatever it says now. Add a comment line above the rows, in the file's style:

```python
        # Then +1 flow and +1 activity from Maou's paper trading desk:
        # TradingDeskFlow and TradingDeskActivities.desk_tick, both on the money
        # flag, so only the money-on row moves.
```

- [ ] **Step 3h: CLAUDE.md.** Append one paragraph at the end of the long "The books (Maou's
  ledger)" bullet under **Key paths**:

```markdown
**Maou's trading desk** (paper mode; spec `docs/superpowers/specs/2026-09-12-maou-trading-desk-design.md`) copies the trading system's `trade_decisions` each weekday from ansaar-data, sizes them into whole-share orders against `capital`, fills them on paper at Yahoo's close and scores them against SHARIABEES in the monthly close (`services/desk_math.py` is the arithmetic, `services/trading_desk.py` the run). Two rules are load-bearing: **a stored close is never overwritten**, because Yahoo rewrites past closes after a split, so the first fetch of a day is the only raw price the desk will ever see; and **a day's findings are stored with its plan**, so a rerun after ansaar recovers cannot resolve a problem that is still true for that day. On stale or odd decisions it trades nothing and raises a `#money` task. Its rules live in the `trading-desk-daily` activities row, read on every run; the seed ships it inactive.
```

- [ ] **Step 4: Run the new tests, then the whole worker suite and the whole core suite, one
  package at a time.** First `<PATHS>` = the four files from Step 2 plus
  `tests/worker/test_registry.py`. Then `tests/worker/` (`<NAME>` = `desk-t8-worker`), then
  `tests/core/` (`<NAME>` = `desk-t8-core`). All must pass. If `test_registry.py` reports
  different counts, the other session merged something. Recount against main; do not pin
  guessed numbers.

- [ ] **Step 5: Prove the tests can fail.** Remove `"TradingDeskActivities": "money_hygiene_enabled"`
  from `ACTIVITY_CLASS_FLAGS`. `test_real_registration_passes_the_boot_check` must fail on a
  money-off row. Restore it.

- [ ] **Step 6: Lint the whole scope CI lints, and commit**

```bash
/home/arshad/Workspace/hikmah/aegis/.venv/bin/ruff check core/src/ tests/core/ worker/src/ tests/worker/
git add core/src/aegis/config.py core/src/aegis/services/integrations_config.py worker/src/aegis_worker/activities/trading_desk.py worker/src/aegis_worker/flows/trading_desk.py worker/src/aegis_worker/registry.py worker/src/aegis_worker/__main__.py config/seed/activities.yaml CLAUDE.md tests/core/test_integrations_read.py tests/worker/activities/test_trading_desk_activity.py tests/worker/flows/test_trading_desk_flow.py tests/worker/test_schedule_sync_mappers.py tests/worker/test_registry.py
git commit -m "feat(desk): schedule Maou's paper trading desk and wire its connection settings"
```

---

## After the plan

These are not tasks for the implementer. They need the owner's approval, and the controller
asks for it:

1. Open a PR from this branch (outward-facing).
2. After merge, deploy with `make aegis-release`: `git pull` both repos first. Validate the boot
   counts and one manual `TradingDeskFlow` run. With the seed inactive and ansaar-data #30 not yet
   live, a manual run should raise `desk_source_error` for ansaar, and that proves the alarm.
3. Set `ansaar_url` in the admin UI. The owner pastes `ansaar_service_secret`.
4. After trading-system-pipeline #355 is fixed and ansaar-data #30 is deployed, set
   `active = true` on `trading-desk-daily` (a production DB write).
