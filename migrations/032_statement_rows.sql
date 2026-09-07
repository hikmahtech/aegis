-- Bank statement rows (spec 2026-09-07-statement-reconciliation-design.md §7).
-- One row per printed transaction line, keyed by a content hash that survives
-- both Axis layouts (§8.3) so an overlapping backfill and live statement dedupe
-- instead of posting the money twice.
--
-- `posted_at` is a marker for the digest, NOT the idempotency ledger:
-- idempotency is the msgid inside the journal block (`stmt/<row_id>`), which
-- survives a crash between the journal commit and this stamp.
--
-- Idempotent DDL: the migration runner keys on the filename and re-runs a
-- renamed file.
CREATE SCHEMA IF NOT EXISTS finance;

CREATE TABLE IF NOT EXISTS finance.statement_rows (
    row_id        text PRIMARY KEY,   -- sha256, see §8.3
    instrument    text NOT NULL,      -- canonical spelling, matches the chart
    occurred_on   date NOT NULL,      -- the transaction date, never the value date
    narration     text NOT NULL,      -- normalised: uppercase, whitespace collapsed
    ref           text,               -- UTR / RRN parsed out of the narration
    direction     text NOT NULL,      -- 'in' | 'out'
    amount        numeric(14,2) NOT NULL,
    balance_after numeric(14,2),      -- running balance; NULL on cards
    statement_id  text NOT NULL,      -- bank + account + period
    file_sha256   text NOT NULL,      -- the source file; a regenerated period is a 2nd file
    matched_msgid text,               -- journal_index.message_id when matched
    candidates    jsonb,              -- msgids an ambiguous row could not choose between
    posted_at     timestamptz,        -- set when post_event wrote a block for this row
    skip_reason   text,               -- 'ambiguous' | 'transfer_counterpart' | 'reversal' | …
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- The matcher's own lookup: everything this account did in a date window.
CREATE INDEX IF NOT EXISTS statement_rows_instrument_day
    ON finance.statement_rows (instrument, occurred_on);

-- Pass 1 of the matcher joins on the reference. Partial and deliberately NOT
-- unique, for the same reasons as `journal_index_ref` (029): most rows carry no
-- reference at all, and the value is issued by someone else's system.
CREATE INDEX IF NOT EXISTS statement_rows_ref
    ON finance.statement_rows (ref)
    WHERE ref IS NOT NULL;
