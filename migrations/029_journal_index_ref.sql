-- The transaction reference (UPI RRN, IMPS reference, SWIFT reference) on the
-- books index. `render_transaction` has always written it into the journal
-- block as a `ref:` tag, and every deterministic parser in `bank_parsers.py`
-- fills `MoneyEvent.ref` — the index simply never stored it. It is the exact
-- key a bank statement carries inside its narration (`UPI/P2A/<ref>/...`,
-- `IMPS-<ref>-...`, `NEFT/<utr>/...`), so a statement reconciler cannot join
-- on anything better. Idempotent DDL: the migration runner keys on the
-- filename and re-runs a renamed file.
ALTER TABLE finance.journal_index ADD COLUMN IF NOT EXISTS ref text;

-- Lookup by reference, for the statement matcher.
--
-- Deliberately NOT UNIQUE. The value is issued by someone else's system — an
-- NPCI RRN, a bank's own IMPS sequence, a SWIFT reference — and this one
-- column holds all three namespaces at once, so nothing here guarantees they
-- cannot collide, and a 12-digit RRN is short enough to recur over years of
-- history. A UNIQUE index would turn any such collision, or a parser that
-- extracts the wrong digits, into a failed INSERT — which drops a real
-- payment out of the index entirely. A duplicate reference costs an ambiguous
-- match the caller can settle on amount and date; that is the cheaper failure.
--
-- Partial, because most rows have no reference at all: the LLM extraction path
-- never sets one (46 of 245 live rows carry an instrument, and `ref` is filled
-- by the same deterministic parsers), so indexing the NULLs would be mostly
-- dead weight.
CREATE INDEX IF NOT EXISTS journal_index_ref
    ON finance.journal_index (ref)
    WHERE ref IS NOT NULL;
