-- Rename the INR-specific money column to a currency-neutral name (issue #52).
-- Metadata-only RENAME COLUMN — instant, preserves all rows.
ALTER TABLE finance.recurring_charge RENAME COLUMN monthly_inr_equivalent TO monthly_home_equivalent;
