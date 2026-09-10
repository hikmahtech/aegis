-- The "reconciled through" watermark, one row per account.
-- Spec: docs/superpowers/specs/2026-09-07-statement-reconciliation-design.md §9.3, §15.10 item 2.
--
-- Once a bank statement has reconciled an account through a date, that
-- statement IS the record for everything up to and including it. An
-- email-lane transaction dated inside that period is a discrepancy or a
-- duplicate of a row the statement already counted, never a fresh posting
-- -- §9.3 calls this the ordering rule, and until now nothing stored the
-- watermark it depends on.
--
-- Idempotent DDL: the migration runner keys on the filename and re-runs a
-- renamed file.
CREATE SCHEMA IF NOT EXISTS finance;

CREATE TABLE IF NOT EXISTS finance.reconciled_through (
    instrument   text PRIMARY KEY,   -- canonical spelling, matches the chart (books.canonical_instrument)
    through_date date NOT NULL,      -- reconciled through this transaction date, inclusive
    statement_id text NOT NULL,      -- the statement that last advanced the watermark
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- The watermark only moves forward. A backfill posts statements in whatever
-- order the operator has them in hand, so reconciling June after July must
-- not un-reconcile July -- callers enforce this with a WHERE guard on the
-- UPDATE, not by reading the row first: a read-then-compare in Python races
-- two concurrent callers, and an INSERT ... ON CONFLICT ... WHERE does not.
