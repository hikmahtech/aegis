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
