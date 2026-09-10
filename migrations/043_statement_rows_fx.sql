-- The original amount of a card charge made abroad (spec
-- 2026-09-07-statement-reconciliation-design.md §8.5).
--
-- An Axis credit-card statement prints the original beside the rupee charge —
-- `( USD 5.89 )   567.87 Dr` — and that is the exact figure the journal block
-- holds. Without these columns the matcher has to convert the rupee amount back
-- through `prices.journal` and a 5% markup band, and production carries no USD
-- or GBP rate at all, so every foreign card row stays unmatched and posts a
-- second time in rupees.
--
-- NULL everywhere else: only the card layout prints it, and `amount` is always
-- the rupee amount that hit the account.
--
-- Idempotent DDL: the migration runner keys on the filename and re-runs a
-- renamed file.
ALTER TABLE finance.statement_rows
    ADD COLUMN IF NOT EXISTS fx_currency text,
    ADD COLUMN IF NOT EXISTS fx_amount numeric(14,2);
