-- One row per parsed bank statement (spec 2026-09-07-statement-reconciliation-design.md §7, §9.3).
--
-- `statement_rows` (032) stores the printed lines and nothing about the
-- statement they came from. That was enough while a human drove the lane from
-- a script and could re-derive the period's opening balance from the first
-- row's running balance -- but a CREDIT CARD statement prints no running
-- balance at all, and prints its opening and closing as figures of their own.
-- Without this table those figures are read at intake and thrown away, and
-- §9.3's closing-balance check -- the point of the whole lane -- has nothing
-- to stand on for exactly the account that needs it most.
--
-- It is also what makes "no statement arrived last month for a declared
-- account" answerable (§15.4's `statement_missing` finding): coverage is a
-- question about statements, and until now only their rows were stored.
--
-- Idempotent DDL: the migration runner keys on the filename and re-runs a
-- renamed file.
CREATE SCHEMA IF NOT EXISTS finance;

CREATE TABLE IF NOT EXISTS finance.statements (
    statement_id    text PRIMARY KEY,   -- bank + account + period, the same id the rows carry
    instrument      text NOT NULL,      -- canonical spelling, matches the chart
    period_start    date NOT NULL,
    period_end      date NOT NULL,
    opening_balance numeric(14,2),      -- NULL when the bank printed none
    closing_balance numeric(14,2),
    file_sha256     text NOT NULL,      -- a regenerated period is a second file
    rows            int NOT NULL DEFAULT 0,
    reconciled_at   timestamptz,        -- set when step 5 posted it and the balance agreed
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Coverage, and the digest's "which statements does this account have?".
CREATE INDEX IF NOT EXISTS statements_instrument_period
    ON finance.statements (instrument, period_end DESC);
