-- The books index (spec 2026-09-05-maou-books-design.md §5.3). The hledger
-- journal is the record; this table is idempotency, receipt<->bank matching,
-- dues dedupe and the admin page. Idempotent DDL: the migration runner keys
-- on filename and re-runs a renamed file.
CREATE TABLE IF NOT EXISTS finance.journal_index (
    message_id        text PRIMARY KEY,
    mailbox           text NOT NULL,
    entity            text NOT NULL,
    kind              text NOT NULL,
    direction         text,
    amount            numeric(14,2),
    currency          text,
    payee             text,
    payee_key         text,
    account           text,
    channel           text,
    instrument        text,
    occurred_on       date,
    due_on            date,
    parser            text NOT NULL,
    confidence        real,
    source_class      text NOT NULL DEFAULT 'other',
    journal_file      text,
    linked_message_id text,
    todoist_ref       text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS journal_index_payee_day
    ON finance.journal_index (payee_key, occurred_on);
CREATE INDEX IF NOT EXISTS journal_index_match
    ON finance.journal_index (currency, amount, occurred_on)
    WHERE kind = 'transaction' AND linked_message_id IS NULL;
