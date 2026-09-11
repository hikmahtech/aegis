"""Step 7 — get statements out of the Drive folder and into `statement_rows`.

Spec §4.1, §6.1. The folder `aegis-accounting/<instrument>/` holds decrypted
statements, one subfolder per declared account. This walks it, parses every
file, and stores the rows the matcher and step 5 read.

**The folder name is a cross-check, never the identifier.** The account is read
from inside the statement by header anchor (§6.1), and a file whose contents
name a different account than its folder is a misfile: reported, and not
imported. That rule earned itself — a substring check on `Credit Card` once
filed nine Axis *current account* statements as credit-card statements, because
a current account contains a `CreditCard Payment` narration row.

Nothing here decides anything about money. It turns files into rows, refuses
what it cannot read, and says what it refused.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from aegis.services import drive
from aegis.services.statements import (
    PARSED,
    ParsedStatement,
    file_digest,
    parse_axis_statement,
    pdf_text,
)
from aegis.services.statements_axis_card import (
    is_axis_card_statement,
    parse_axis_card_statement,
)
from aegis.services.statements_hdfc import parse_hdfc_statement

logger = structlog.get_logger()

#: What a file is, decided by its bytes rather than its name. A statement
#: renamed by hand — which is the normal way one arrives in a Drive folder —
#: still has to be readable.
_PDF_MAGIC = b"%PDF"

MISFILED = "misfiled"
UNREADABLE = "unreadable"


@dataclass
class FileOutcome:
    file_id: str
    title: str
    folder: str
    status: str
    reason: str = ""
    statement_id: str = ""
    rows: int = 0


@dataclass
class IntakeReport:
    outcomes: list[FileOutcome] = field(default_factory=list)
    stored: int = 0
    skipped_existing: int = 0
    #: Rows already stored whose narration or `ref` a newer parser read better
    #: (#428). Separate from `stored` because nothing NEW arrived — reporting
    #: them together would read as a statement growing rows on a re-read.
    refreshed: int = 0

    @property
    def failures(self) -> list[FileOutcome]:
        return [o for o in self.outcomes if o.status != PARSED]


def parse_bytes(
    data: bytes, *, title: str = "", declared: Collection[str] = ()
) -> ParsedStatement:
    """One downloaded file as a parsed statement, whichever bank wrote it.

    Dispatched on the bytes: a PDF is Axis (HDFC mails no attachment and its
    SmartStatement arrives as HTML), anything else is tried as HDFC HTML. The
    file's NAME is deliberately not consulted — see the module docstring.

    Axis sends two different PDFs, and the **header anchor** picks between them
    (§6.1) — never a substring, and never the title. `is_axis_card_statement`
    asks one question: does the file carry the card's whole-line
    `Card No: … Name …` anchor? A savings or current statement does not, even
    the ones carrying a `CreditCard Payment` narration row, which is the
    misfiling this rule exists to stop. Asking before parsing rather than
    falling back afterwards also keeps the card parser's own refusal reason:
    a card statement that fails its arithmetic must say so, not come back
    `no_header_anchor` from the bank-account parser.
    """
    sha = file_digest(data)
    if data[:4] == _PDF_MAGIC:
        text = pdf_text(data)
        axis = parse_axis_card_statement if is_axis_card_statement(text) else parse_axis_statement
        return axis(text, file_sha256=sha, subject=title, declared=declared)
    return parse_hdfc_statement(
        data.decode("utf-8", "replace"), file_sha256=sha, subject=title, declared=declared
    )


async def store_rows(pool: Any, statement: ParsedStatement) -> tuple[int, int, int]:
    """Persist one statement's rows. Returns (stored, unchanged, refreshed).

    `row_id` is a content hash over instrument, date, direction, amount,
    balance and occurrence index, so the conflict clause is the whole
    idempotency story: a re-sent statement, an overlapping period, and the same
    file downloaded twice all collapse, while two identical payments on one day
    stay two rows because their occurrence index differs.

    **`narration` and `ref` are refreshed; nothing else is.** This used to be a
    flat `DO NOTHING`, on the reasoning that a stored row's parsed columns
    cannot have changed because they are what the hash is over. That is not
    true of these two. §8.3 keys a BANK row on its running balance and
    deliberately not on its narration — a narration-keyed id would hash the
    same transaction twice across the two Axis layouts — so narration, and the
    `ref` read out of it, are the parsed columns a better parser CAN change.
    #428 was exactly that: the FY2024-25 statement's first 23 rows were stored
    with 16 characters cut off the front and a NULL `ref`, and with `DO NOTHING`
    the fix could never reach them however many times the file was re-read.

    A card row is unaffected either way: it has no running balance, so
    `row_id_for` falls back to the narration and a changed one is a different
    row, never a conflict to refresh.

    What must NOT be touched is the work done to a row since it was stored —
    `matched_msgid`, `posted_at`, `skip_reason` — which is why this sets two
    named columns rather than the row. `ref` COALESCEs for the same reason
    `journal_index.upsert` does: a reference is a fact about the payment, not a
    verdict about it, so a re-read that cannot find one must not erase what a
    better parser already did.

    The `WHERE` matters at this size. Without it every intake run would rewrite
    all ~2,800 stored rows to the values they already hold, twice a day, for
    nothing but table bloat.
    """
    if not statement.rows:
        return 0, 0, 0
    stored = refreshed = 0
    for row in statement.rows:
        # True = inserted, False = refreshed, None = already correct. `xmax = 0`
        # is how a RETURNING clause tells an insert from an update.
        inserted = await pool.fetchval(
            """
            INSERT INTO finance.statement_rows AS sr
              (row_id, instrument, occurred_on, narration, ref, direction, amount,
               balance_after, statement_id, file_sha256, fx_currency, fx_amount)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT (row_id) DO UPDATE
               SET narration = EXCLUDED.narration,
                   ref       = COALESCE(EXCLUDED.ref, sr.ref)
             WHERE sr.narration IS DISTINCT FROM EXCLUDED.narration
                OR sr.ref      IS DISTINCT FROM COALESCE(EXCLUDED.ref, sr.ref)
            RETURNING (xmax = 0)
            """,
            row.row_id, row.instrument, row.occurred_on, row.narration, row.ref,
            row.direction, row.amount, row.balance_after, row.statement_id,
            row.file_sha256, row.fx_currency, row.fx_amount,
        )
        if inserted is True:
            stored += 1
        elif inserted is False:
            refreshed += 1
    if refreshed:
        logger.info(
            "statement_rows_refreshed",
            statement=statement.statement_id,
            rows=refreshed,
        )
    return stored, len(statement.rows) - stored - refreshed, refreshed


async def store_statement(pool: Any, statement: ParsedStatement) -> None:
    """Persist the statement itself, beside its rows.

    The period's opening and closing balances live here and nowhere else. A
    bank statement's could be re-derived from the first row's running balance,
    but a card's cannot — a card prints no running balance and states its
    opening and closing as figures of their own — so throwing them away at
    intake would leave §9.3's check with nothing to stand on for cards.

    `DO UPDATE` rather than `DO NOTHING`, unlike the rows. A statement row is
    immutable content (its id is a hash of it) and may have been matched or
    posted since, so re-importing must not erase that work. A statement's own
    figures are the same figures on a re-import of the same period, and a bank
    that reissues a corrected statement for a period we have already read
    should win — but `reconciled_at` is left alone, because whether we
    reconciled it is our record, not the bank's.
    """
    await pool.execute(
        """
        INSERT INTO finance.statements
          (statement_id, instrument, period_start, period_end,
           opening_balance, closing_balance, file_sha256, rows)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        ON CONFLICT (statement_id) DO UPDATE
        SET opening_balance = EXCLUDED.opening_balance,
            closing_balance = EXCLUDED.closing_balance,
            file_sha256     = EXCLUDED.file_sha256,
            rows            = EXCLUDED.rows
        """,
        statement.statement_id, statement.instrument,
        statement.period_start, statement.period_end,
        statement.opening_balance, statement.closing_balance,
        statement.file_sha256, len(statement.rows),
    )


async def intake_folder(
    pool: Any,
    token_path: Path,
    folders: Mapping[str, str],
    *,
    declared: Collection[str] = (),
    dry_run: bool = False,
) -> IntakeReport:
    """Walk `folders` (instrument -> Drive folder id) and store what parses.

    One file's failure never stops the walk: a locked PDF, a bank that changed
    its layout and a misfile are each recorded and skipped, because a statement
    that cannot be read is a thing to report — never a reason to abandon the
    eleven that can.
    """
    report = IntakeReport()
    svc = drive._build_drive_service(token_path)
    for instrument, folder_id in sorted(folders.items()):
        for f in drive._list_folder(svc, folder_id):
            outcome = FileOutcome(
                file_id=f.get("id", ""), title=f.get("name", ""), folder=instrument,
                status=PARSED,
            )
            try:
                data = drive._download(svc, f)
                statement = parse_bytes(data, title=outcome.title, declared=declared)
            except Exception as exc:  # noqa: BLE001 — one bad file, not the walk
                outcome.status, outcome.reason = UNREADABLE, f"{type(exc).__name__}: {exc}"[:200]
                report.outcomes.append(outcome)
                continue

            # `PARSED`, not a "ok" string of this module's own invention. The
            # first cut hardcoded "ok" and threw away all 15 real statements as
            # failures — and its tests agreed, because the fixtures invented the
            # same wrong constant. Import the vocabulary from the module that
            # defines it and neither can drift.
            if statement.status != PARSED:
                outcome.status, outcome.reason = statement.status, statement.reason
                report.outcomes.append(outcome)
                continue

            # §4.1: the folder is a cross-check. A file whose contents name a
            # different account than its folder is a misfile — reported, and
            # NOT imported, because importing it would put one account's money
            # on another's balance and the closing-balance check would then
            # revert a statement that was never wrong.
            if statement.instrument != instrument:
                outcome.status = MISFILED
                outcome.reason = (
                    f"the file says {statement.instrument}, the folder says {instrument}"
                )
                report.outcomes.append(outcome)
                continue

            outcome.statement_id = statement.statement_id
            outcome.rows = len(statement.rows)
            if not dry_run:
                await store_statement(pool, statement)
                stored, existing, refreshed = await store_rows(pool, statement)
                report.stored += stored
                report.skipped_existing += existing
                report.refreshed += refreshed
            report.outcomes.append(outcome)

    logger.info(
        "statement_intake_done",
        files=len(report.outcomes),
        failures=len(report.failures),
        stored=report.stored,
        existing=report.skipped_existing,
        refreshed=report.refreshed,
    )
    return report
