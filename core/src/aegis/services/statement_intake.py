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
        parse = (
            parse_axis_card_statement if is_axis_card_statement(text) else parse_axis_statement
        )
        return parse(text, file_sha256=sha, subject=title, declared=declared)
    return parse_hdfc_statement(
        data.decode("utf-8", "replace"), file_sha256=sha, subject=title, declared=declared
    )


async def store_rows(pool: Any, statement: ParsedStatement) -> tuple[int, int]:
    """Persist one statement's rows. Returns (stored, already present).

    `row_id` is a content hash over instrument, date, direction, amount,
    balance and occurrence index, so `ON CONFLICT DO NOTHING` is the whole
    idempotency story: a re-sent statement, an overlapping period, and the same
    file downloaded twice all collapse, while two identical payments on one day
    stay two rows because their occurrence index differs.

    Deliberately DO NOTHING rather than DO UPDATE. A row that is already here
    may have been matched or posted since — `matched_msgid`, `posted_at`,
    `skip_reason` — and re-importing the file must not erase that work. The
    parsed columns cannot have changed anyway: they are what the hash is over,
    and the two that are not — `fx_currency`/`fx_amount` — are read out of the
    narration, which is itself in the hash on the card rows that carry them.
    """
    if not statement.rows:
        return 0, 0
    stored = 0
    for row in statement.rows:
        result = await pool.execute(
            """
            INSERT INTO finance.statement_rows
              (row_id, instrument, occurred_on, narration, ref, direction, amount,
               balance_after, statement_id, file_sha256, fx_currency, fx_amount)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT (row_id) DO NOTHING
            """,
            row.row_id, row.instrument, row.occurred_on, row.narration, row.ref,
            row.direction, row.amount, row.balance_after, row.statement_id,
            row.file_sha256, row.fx_currency, row.fx_amount,
        )
        if result.endswith("1"):
            stored += 1
    return stored, len(statement.rows) - stored


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
                stored, existing = await store_rows(pool, statement)
                report.stored += stored
                report.skipped_existing += existing
            report.outcomes.append(outcome)

    logger.info(
        "statement_intake_done",
        files=len(report.outcomes),
        failures=len(report.failures),
        stored=report.stored,
        existing=report.skipped_existing,
    )
    return report
