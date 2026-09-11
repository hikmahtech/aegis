# Maou's trading desk, paper mode — design

**Date:** 2026-09-12
**Status:** design approved in conversation; not built
**Owner lane:** Maou / money
**Builds on:** `2026-09-05-maou-books-design.md` (the books), the problem hub
(`2026-09-07-problem-hub-design.md`), the monthly close (`MonthCloseFlow`)
**Depends on:** trading-system-pipeline #355 (equity decisions stopped on 2026-08-26),
trading-system-pipeline #356 (ADR-0052 amendment), ansaar-data #30 (serve decisions by date)

---

## 1. Why

The owner wants Maou to invest for them, and eventually to buy and sell on Zerodha by itself.
Nothing should trade real money before it has a paper record, so the first step is a desk that
trades on paper exactly as it would trade live, and keeps score.

The split, agreed on 2026-09-12:

- **The trading system decides what to hold.** Signals, the halal filter, risk limits and kill
  switches produce a daily target portfolio, `trading_system.trade_decisions` (ADR-0052 Phase A).
- **Maou decides how much, and whether.** Maou sizes the orders from a capital amount, refuses
  to trade on data that looks wrong, places the orders (on paper now, on Zerodha later), and
  keeps an independent record and score.

Two facts shape what the score can do:

- **It cannot prove a small edge quickly.** The equity backtest implies about 6% a year over the
  benchmark. An edge that size takes roughly ten years of live results to show at a t-statistic
  of 2. In months, the score can show whether live results are in line with the backtest or
  clearly worse, and it catches the pipeline when it breaks.
- **The pipeline breaks silently.** `trade_decisions` has stopped three times: #83 (from
  2026-05-07), #150 (from 2026-07-03) and #355 (equity, from 2026-08-26). Each time its own
  checks passed. An independent consumer that refuses stale or odd input is the cheapest alarm.

## 2. Scope

**In scope:** paper trading of equity and ETF decisions: copy, size, check, fill, value, tax,
score. Also a monthly report section and hub problems.

**Out of scope, each in a later spec:**

- Live orders on Zerodha Kite (§15 lists the seams this design leaves for it).
- The owner's investment plan: monthly amount, emergency fund, the long-term SIP core.
- Holdings from the CDSL eCAS.
- Grading and explaining decisions. The owner decided on 2026-09-12 that the pipeline closes each
  decision and records its outcome, and ansaar-data serves both. A separate AEGIS lane writes a
  post-mortem on each losing decision and a weekly review that files improvements as pipeline
  issues. That lane uses the model; the desk does neither.
- Crypto: turned off by config (India taxes it at 30% with 1% TDS on each sale and no loss
  set-off, and its model was retrained on 2026-09-10 and has not been re-verified).

**Never in scope:** the model choosing stocks, or Maou suggesting trades in chat. The model writes
nothing on this desk. Every number is computed by code.

## 3. The daily run

`TradingDeskFlow` runs on weekdays at 08:00 IST (`30 2 * * 1-5`), before the market opens at 09:15.
One idempotent activity does the steps below in order. Running it twice in a day changes nothing.

1. **Find the day.** The last NSE trading day is the latest Yahoo bar date for `^NSEI` strictly
   before today (IST). This needs no holiday calendar. If Yahoo fails here, the run stops after
   step 6 with `desk_source_error`, and no plan row is written, because there is no date to write
   it against.
2. **Copy.** Fetch the decisions for exactly that date from ansaar
   (`GET /api/execution/trade-decisions?date=...`). Insert them into `finance.desk_decisions` with
   `ON CONFLICT DO NOTHING`. A copy is never updated, so the record stays as it was served even if
   the pipeline later rewrites its history. The copy is made whatever the checks later decide.
3. **Prices.** Fetch recent daily bars from Yahoo for every held symbol, every symbol in the
   copied decisions, every symbol with a pending order, and both benchmarks. Store them in
   `finance.desk_prices`. A stored close is **never overwritten**, because Yahoo rewrites past
   closes after a split: RELIANCE's 2024 bonus shows as about ₹1,338 for days that traded at about
   ₹2,677. So the first fetch of a day, made the morning after it, is the raw price the desk needs,
   and a later fetch would be split-adjusted. A bar for today or later is never stored, because it
   could be an intraday price. Split and dividend events are filled in if a later fetch has them
   and the stored row doesn't.
4. **Fill.** Fill each pending paper order at the close of its fill day (§6). If that close is not
   out yet, leave the order pending.
5. **Plan.** If no plan exists yet for that date and no order is pending, run the checks (§5). If
   they pass, turn the decisions into orders (§4). Write one `finance.desk_plans` row for the date
   whatever the outcome, in the same transaction as its orders, so a date is acted on once.
6. **Report.** Reconcile the daily problem classes on the hub (§10).

The pipeline writes decisions for weekend dates too (Saturday 2026-09-05 and Sunday 09-06 have
rows). The desk only ever reads the decisions for the last trading day, so those rows are ignored.

**Findings are stored with the plan.** A plan row keeps the findings its step produced. A second run
on the same day re-raises exactly those, instead of re-deriving them. If it re-derived them, a rerun
after ansaar recovered would resolve a problem that is still true for that day.

## 4. Sizing orders

- **Portfolio value** `V` = cash + Σ quantity × close on the decision date.
- **Target value** per name = `target_weight` × `V`. Weights for asset classes not in
  `asset_classes` are left as cash, never spread over the rest, so the pipeline's risk sizing stays
  as it decided.
- **When to trade a name.** Trade if any of these is true:
  - the target is zero and something is held (a full exit)
  - nothing is held and the target is above zero (a new entry)
  - the gap between target and current value is more than `max(band_abs × V, band_rel × target)`

  The pipeline rebuilds its equity targets daily. Without this band, every small change would pay
  costs and short-term tax.
- **Whole shares.** Buys are rounded down to whole shares at the decision-date close. A name whose
  single share costs more than its target is skipped, with the reason `below_one_share`. A partial
  sell is rounded to the nearest share, and a full exit sells everything.
- **Sells before buys.** Sale proceeds are available for buys on the same day, as they are at
  Zerodha. Buys go in `selection_rank` order until the cash, after estimated costs, runs out. Ties,
  such as rank 1 in two asset classes, go to the larger `target_weight`, then to the symbol. The
  buy that doesn't fit is cut down to what fits, and the rest are skipped.
- **Order cap.** No single buy may exceed `max_order_pct × V`. The pipeline never sizes a name
  that large, so a bigger weight makes the whole day suspect (§5).

## 5. Checks before trading

If a check fails, the desk trades nothing that day and holds what it has. It records the outcome in
`desk_plans` and raises a problem. When the input is uncertain, doing nothing and telling the owner
is the safe default. Dumping a portfolio because of a pipeline glitch is the failure this rules out.

| Check | Fails when | Outcome | Problem class |
|---|---|---|---|
| Fresh | ansaar returns no rows for the last trading day | `held_stale` | `desk_decisions_stale` |
| Complete | a class the desk holds now has no rows today, and no row carries a kill condition or a non-`NORMAL` recovery state | `held_suspect` | `desk_decisions_suspect` |
| Sane | enabled weights sum to more than 1.0 + 1e-6, or any weight is ≤ 0 or above `max_order_pct` | `held_suspect` | `desk_decisions_suspect` |
| Halal | a row has `halal_status != 'COMPLIANT'` or `direction != 'LONG'` | the row is dropped and never bought; the rest trades | `desk_decisions_suspect` |
| Reachable | the ansaar token or request fails | `held_stale` | `desk_source_error` |

An empty day is ambiguous: the pipeline writes zero rows both when it fails and when its risk
manager halts everything (COOLING). The desk cannot tell these apart, so it holds and asks. The owner
checks `kill_switch_events`. A later change to ansaar-data #30 can put the halt reason in the
response.

## 6. Paper fills and valuation

- **Fill day:** the first NSE trading day on or after the day the order was created. An order
  created at 08:00 on a trading day fills at that day's close, and the next morning's run records
  it.
- **Fill order:** sells fill before buys. Prices move between planning and the fill, so a buy
  that would take cash below zero is cut to what fits. If no share fits, it is cancelled with the
  reason `no_cash`.
- **Fill price:** Yahoo's raw close for the fill day. If Yahoo has no bar, use ansaar's price for
  that date and set `price_source = 'ansaar'`. The ansaar prices endpoints return rows newest
  first and `volume` as a string. If neither source has a price three trading days after the fill
  day, cancel the order with the reason `price_missing` and raise `desk_price_missing` for that
  symbol.
- **Why Yahoo first:** the system being scored should not supply its own prices, and ansaar's
  `eod_prices` has known bad ticks (trading-system-pipeline #255).
- **Splits and bonuses:** Yahoo reports them as split events (a 1:1 bonus is a 2:1 split). From
  the effective date, held quantity is multiplied by the ratio, and each FIFO lot's cost per share
  is divided by it.
- **Dividends:** credited to paper cash on the ex-date, at quantity held × Yahoo's per-share
  amount. `ponytail:` a dividend Yahoo misses is simply lost, which slightly understates the
  return.
- **Value:** quantity × raw close. A holding that moves more than 50% between two consecutive
  closes is flagged in the report as a possible bad price or missing split event.

## 7. Costs and tax

- **Costs** (India, delivery, Zerodha): `cost_pct_per_side` (default 0.2%) on every fill. This
  covers STT, stamp duty, exchange fees, GST and some slippage. Each sell also pays
  `sell_charge_inr` (default ₹16) for the DP charge. On a ₹10,000 position that flat charge is
  another 0.16%, which is why paper uses realistic capital.
- **Tax** on realised gains, FIFO per symbol, per Indian financial year (April to March):
  - Short-term (held under 12 months) uses `tax_rate[asset_class]`: equity 20%, and ETF 30%.
    Gold and silver ETF short-term gains are taxed at the slab rate. An equity ETF's are taxed at
    20%, so 30% overstates the tax on those. Conservative on purpose; per-symbol rates can come
    later.
  - Long-term uses `ltcg_rate` (12.5%). The `ltcg_exemption_inr` (₹1,25,000) a year applies to
    equity alone: section 112A covers listed shares and equity-oriented units, so a gold or
    silver ETF gets none of it.
  - `ponytail:` conservative. There's no netting of losses across classes and no loss
    carry-forward, and the exemption counts the desk alone. Both make the desk look slightly worse
    than it would be. Add netting and carry-forward if the gap ever matters to a decision.
- **After-tax value** = portfolio value − tax owed on realised gains for every financial year so far.
  The benchmark is not taxed: held long-term, its gains would likely fall under the exemption.

## 8. The score

- **Start:** the first fill date. Paper capital is `capital` (default ₹1,00,000).
- **Benchmark:** `SHARIABEES.NS` (the Nifty 50 Shariah ETF), the fair comparison for a halal
  investor. It's bought with the same capital at the start close and pays one buy cost. From then
  on it's held the way the desk holds: its splits adjust the units, and its dividends are paid as
  cash. It trades thinly; a missing close uses the last close on or before that date.
- **No adjusted closes anywhere.** Yahoo rescales its adjusted close after every later dividend,
  so a value stored in September and one fetched in December are in different scales, and their
  ratio is wrong by the dividend.
- **Context:** `^NSEI` (Nifty 50) as a price index, shown and never used for a verdict.
- **Weekly excess:** the desk's weekly return (after costs, before tax) minus SHARIABEES's, measured
  from the last close of one ISO week to the last close of the next.
- **Statistics:** `n` weeks, mean, standard deviation, and t = mean / sd × √n.
- **Label, in words:**

  | Condition | Label |
  |---|---|
  | n < 12 | too early |
  | t ≤ −2 | clearly behind the benchmark |
  | −2 < t ≤ −1 | behind the benchmark |
  | −1 < t < 1 | no evidence yet |
  | 1 ≤ t < 2 | suggestive |
  | t ≥ 2 | strong |

- **Warning check** (monthly, at the close): the expected weekly excess is
  `e = (1 + expected_excess_pa)^(1/52) − 1`. The check fires when `n ≥ 12` and
  `mean + 2 × sd/√n < e`, meaning live results are more than two standard errors below what the
  backtest promised. It raises `desk_below_expectation`. The default for `expected_excess_pa`
  (0.06) comes from the top-15 backtest (ADR-0062), and `trade_decisions` adds risk overlays on top
  of that book. So the owner should set this from the trading system's own figure once there is
  one.

## 9. What the owner sees

A **Trading desk** section in the monthly close. Made-up example:

```
Trading desk (paper): 14 weeks since 15 Sep. Capital ₹1,00,000.
Value ₹1,04,230 (after tax ₹1,03,410)   SHARIABEES ₹1,02,100   Nifty 50 ₹1,01,300
Weekly gap to SHARIABEES: +0.15% on average, t = 0.8: no evidence yet
Holding (paper) 9 names, 12% cash: TCS, INFY, HCLTECH, GOLDBEES, ...
This month: 23 orders, ₹612 in costs.
Days held back: 2 (1 stale, 1 suspect). Prices from ansaar: 1.
Check: XYZ moved −51% on 3 Nov. Possible missing split.
```

No trade suggestions. The holdings are shown because they're public on ansaar.in anyway, and
they're labelled paper.

## 10. Problems

Problems go through `hub_watch.reconcile_findings` with `source = "money"` and
`subject_kind = "trading_desk"`, so each becomes a `#money @maou @next` task in Finance (the owner
map in `hub_project`). A problem resolves itself on the first run that no longer finds it.

| Class | Subject | Reconciled by |
|---|---|---|
| `desk_decisions_stale` | `decisions` | the daily run |
| `desk_decisions_suspect` | `decisions` | the daily run |
| `desk_source_error` | `ansaar` or `yahoo` | the daily run |
| `desk_price_missing` | the symbol | the daily run |
| `desk_below_expectation` | `desk` | the monthly close only, so while it holds it comes back once a month, not every day |

## 11. Data

`migrations/045_trading_desk.sql` (renumber if another PR takes 045 first):

```sql
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
CREATE INDEX IF NOT EXISTS desk_orders_pending ON finance.desk_orders (created_at)
    WHERE status = 'pending';

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

- **`seq`** is an order's place in its plan: sells first, then buys in rank order. Fills follow it.
  **`created_day`** is the IST date the order was planned, which is what its fill day counts from.
- **Derived on read, never stored:** positions, cash, lots, value history, tax. The number of fills
  is small (hundreds a year), and a stored copy would be one more thing to drift.
- **Symbols:** `desk_prices.symbol` uses Yahoo's form (`<NSE symbol>.NS`, or `^NSEI`). Orders and
  decisions use the NSE symbol.

## 12. Configuration

**Connection:** two integration keys in `integrations_config.CONFIG_REGISTRY`, edited on the admin
Integrations page, with matching `Settings` fields defaulting to empty:

- `ansaar_url`: this deployment uses `http://ansaar-data:3000`, the worker's overlay network.
- `ansaar_service_secret`: secret, stored encrypted. Used with `POST /api/auth/client-token` for a
  15-minute token, fetched once per run. Never the admin login (ansaar-data #25).

**Desk rules:** the `config` of the `trading-desk-daily` activities row, read from the DB by the
activity and by the monthly close. It is not passed through the workflow input, because
`trigger_workflow` ignores activity config.

```yaml
mode: paper               # 'live' is refused until the live spec lands
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
```

**Gates:**
- The seed row ships `active: false`, so a fork never runs it.
- The FlowSpec uses `feature_flag="money_hygiene_enabled"`.
- With `ansaar_url` empty, the activity logs `trading_desk_unconfigured` and does nothing.

## 13. Code layout

| File | What it holds |
|---|---|
| `core/src/aegis/connectors/ansaar.py` | `AnsaarClient`: `decisions(day) -> (rows, meta)` and `prices(symbol, asset_class, start, end)`, each fetching the client token on first use |
| `core/src/aegis/connectors/finance.py` | `FinanceConnector.daily_bars(symbol, start, end)`: bars plus split and dividend events, next to the existing quote provider |
| `core/src/aegis/services/desk_math.py` | Pure functions, no I/O: last trading day, checks, order sizing, split adjustment, FIFO, value history, tax by year, weekly statistics, label, warning check |
| `core/src/aegis/services/trading_desk.py` | Database reads and writes, `run_tick(pool, *, ansaar, finance, today, project)`, `month_summary(pool, month_first, next_first)`, `reconcile_expectation`, config loading |
| `worker/src/aegis_worker/activities/trading_desk.py` | `TradingDeskActivities.desk_tick`: one activity (new class, so a constructor in `main()` and an entry in `collect_activities`) |
| `worker/src/aegis_worker/flows/trading_desk.py` | `TradingDeskFlow`, `TradingDeskConfig(agent_id)` |
| `worker/src/aegis_worker/registry.py` | One `FlowSpec` |
| `config/seed/activities.yaml` | The `trading-desk-daily` row (Maou, inactive) |
| `worker/src/aegis_worker/activities/money.py`, `money_render.py` | Monthly close: the desk section and the warning check |
| `migrations/045_trading_desk.sql` | §11 |
| `core/src/aegis/services/integrations_config.py`, `core/src/aegis/config.py` | §12 connection keys |
| `CLAUDE.md` | One paragraph in the books section describing the lane |

## 14. Testing

Tests need a real Postgres and no DB mocks, the same as the rest of the repo.

- **`desk_math`, against hand-worked numbers:**
  - Sizing: whole shares, the band, a full exit, a new entry, `below_one_share`, sells before buys,
    and a buy cut down to the remaining cash.
  - A 2:1 split between buy and sell, adjusting quantity and FIFO cost.
  - A dividend credit.
  - FIFO across two lots, with the 12-month short/long boundary.
  - Tax across 31 March.
  - Weekly statistics on a known series (mean, sd, t), every label including the negative ones,
    and the warning check on both sides of its boundary.
- **Checks:** a stale date; an empty day; a held class that vanishes with no reason (suspect); a
  vanished class with a kill condition (not suspect); weights summing above 1; a non-compliant row
  (dropped, and the rest trades).
- **Connectors (respx):**
  - ansaar: token, decisions including the empty-day shape, prices returned newest first with a
    string `volume`.
  - Yahoo: a `None` close, split and dividend events.
- **Activity, DB-backed:**
  - Day 1 plans orders, day 2 fills them, and the value is right.
  - A second run on the same day makes no new orders.
  - A stale day holds and raises exactly one problem, and the next good day resolves it.
- **Falsifiability:** break-and-revert one sizing test and one check test before calling the suite
  done. See the unfalsifiable-test lesson.

## 15. Live mode later: the seams

Live mode is not built here. This design keeps the step to live small:

- `desk_orders.mode` and `broker_order_id` already exist. Live mode replaces only the fill step
  (§3, step 4): CNC (delivery) orders go to Kite, and fills come from Kite's order history.
- **Approval:** Kite's access token expires daily, so each trading day needs a login. Maou sends a
  Slack card (`InteractionFlow`) that leads to the Zerodha login, and the redirect back to AEGIS
  stores the day's token. The login is the approval. No login means no orders.
- **Static IP:** under SEBI's 2025 retail algo framework, API orders must come from a static IP
  registered with Zerodha, so the live spec must provide one.
- **Books:** each live fill is posted to the journal (`assets:investments:zerodha:<symbol>`) and
  checked against Zerodha's contract-note email and the monthly eCAS.
- **Cash:** deposits and withdrawals need a `desk_cash_moves` table. Paper capital is fixed, so this
  design has none.
- **Risk loop:** once live, the real portfolio value lives here. Whether the pipeline's RiskManager
  reads it back is the open question in trading-system-pipeline #356.
- **Checks:** every check in §5 applies unchanged. Setting `mode` back to `paper` is the kill
  switch.

## 16. Rollout

1. Build and merge. The tests use fixtures, so this does not wait on the pipeline.
2. Trading-system-pipeline #355 is fixed and equity decisions flow again.
3. ansaar-data #30 is deployed.
4. The owner sets `ansaar_url` and pastes `ansaar_service_secret` on the admin Integrations page.
5. Set `active = true` on `trading-desk-daily` (a DB write; ask first).
6. Validate on the first run:
   - it copied the last trading day's decisions, and read those rows: they must include
     equities. Until pipeline #355 is fixed only ETF rows arrive, and nothing holds the desk
     back — the checks pass and it buys ETFs. The missing-equity check cannot catch that on
     day one, because it fires on an equity the desk holds and on day one it holds nothing.
   - the first plan matches a hand calculation from the rows that were copied
   - the next morning filled the orders at Yahoo's close
