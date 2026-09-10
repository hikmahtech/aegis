"""Statement intake and reconciliation activities (spec §5, §8, §9, §15.4).

The lane had every piece except a way to run itself: intake, the matcher and
step 5's posting were driven by hand from one-off scripts inside the worker
container. These two activities are what `StatementReconcileFlow` calls, and
they are deliberately coarse — the whole match runs inside ONE activity rather
than one per statement, because 2,580 statement rows and their journal
candidates are not a Temporal payload, and passing them between activities
would put the whole ledger through the workflow history.

Which account's Google token opens the Drive folder is `statement_account`.
It is the same token directory Gmail and Drive already use, and the folder
needs the `drive.readonly` scope (spec §5.5) — a missing scope degrades to a
reported failure, never a silent zero.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from temporalio import activity

#: The settings row holding `{"accounts": {instrument: {folder_id, entities,
#: post_entity}}}`. One row rather than one setting per account: the folder ids
#: and the entity scope are read together on every run and drift apart if they
#: are edited apart.
FOLDER_SETTING = "integration:statement_folders"


async def _folder_config(pool: Any) -> dict:
    """The account map, or `{}` when the lane has never been configured.

    An unconfigured lane is not an error — a fork of AEGIS has no Drive folder
    and must not fail a scheduled flow to say so.
    """
    raw = await pool.fetchval("SELECT value FROM settings WHERE key = $1", FOLDER_SETTING)
    if raw is None:
        return {}
    cfg = raw if isinstance(raw, dict) else json.loads(raw)
    accounts = cfg.get("accounts")
    return accounts if isinstance(accounts, dict) else {}


@dataclass
class StatementActivities:
    db_pool: Any
    gmail_token_dir: str
    #: A `BooksConfig`; None means the books are not configured and there is
    #: nothing to reconcile against.
    books_cfg: Any = None
    #: The Google account whose token opens the accounting folder.
    statement_account: str = "arshad-hikmah"

    def _token_path(self) -> Path:
        return Path(self.gmail_token_dir) / f"{self.statement_account}.json"

    @activity.defn
    async def intake_statements(self, dry_run: bool = False) -> dict:
        """Walk the Drive folders and store what parses (spec §5, §6).

        Returns the per-file outcomes as well as the counts: a file that failed
        is the thing worth reporting, and a caller that only gets numbers has
        to go and look the failures up.
        """
        from aegis.services import books, statement_intake

        accounts = await _folder_config(self.db_pool)
        if not accounts:
            return {"status": "skipped", "reason": "no_statement_folders", "stored": 0}

        declared = (
            await books.declared_accounts(self.books_cfg) if self.books_cfg else frozenset()
        )
        folders = {
            instrument: acct["folder_id"]
            for instrument, acct in accounts.items()
            if acct.get("folder_id")
        }
        report = await statement_intake.intake_folder(
            self.db_pool,
            self._token_path(),
            folders,
            declared=declared,
            dry_run=dry_run,
        )
        return {
            "status": "ok",
            "files": len(report.outcomes),
            "stored": report.stored,
            "existing": report.skipped_existing,
            "failures": [
                {
                    "file": o.title,
                    "folder": o.folder,
                    "status": o.status,
                    "reason": o.reason,
                }
                for o in report.failures
            ],
        }


async def load_statements(pool: Any) -> list[Any]:
    """Every stored statement, rebuilt with its rows attached.

    Read from `finance.statements` and `finance.statement_rows` rather than
    re-parsed from Drive: the figures a statement proves itself with are stored
    at intake, and re-downloading twenty PDFs on every tick to recover them
    would make the schedule cost a Drive quota it does not need to spend.
    """
    from aegis.services.statements import PARSED, ParsedStatement, StatementRow

    rows: dict[str, list[Any]] = {}
    for r in await pool.fetch(
        "SELECT * FROM finance.statement_rows ORDER BY statement_id, occurred_on, row_id"
    ):
        rows.setdefault(r["statement_id"], []).append(
            StatementRow(
                row_id=r["row_id"], instrument=r["instrument"], occurred_on=r["occurred_on"],
                narration=r["narration"], ref=r["ref"], direction=r["direction"],
                amount=r["amount"], balance_after=r["balance_after"],
                statement_id=r["statement_id"], file_sha256=r["file_sha256"],
            )
        )
    out = []
    for s in await pool.fetch(
        "SELECT * FROM finance.statements ORDER BY instrument, period_end"
    ):
        out.append(
            ParsedStatement(
                status=PARSED,
                instrument=s["instrument"],
                period_start=s["period_start"],
                period_end=s["period_end"],
                opening_balance=s["opening_balance"],
                closing_balance=s["closing_balance"],
                rows=tuple(rows.get(s["statement_id"], ())),
                statement_id=s["statement_id"],
                file_sha256=s["file_sha256"],
            )
        )
    return out
