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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
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
        # §15.4: the statement-scoped classes get their OWN `reconcile_findings`
        # call. Folded into the instrument-scoped one, each set would resolve
        # the other — neither appears among the other's findings.
        from aegis.services import statement_findings

        swept = await statement_findings.sweep(
            self.db_pool,
            statement_findings.intake_findings(report),
            kinds=(statement_findings.STATEMENT,),
        )
        return {
            "status": "ok",
            "files": len(report.outcomes),
            "stored": report.stored,
            "existing": report.skipped_existing,
            "findings": {
                k: {
                    "fresh": len(v.get("fresh") or []),
                    "resolved": len(v.get("resolved") or []),
                }
                for k, v in swept.items()
            },
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

    @activity.defn
    async def reconcile_statements(self, post: bool = False, since: str = "") -> dict:
        """Match every stored row, post what is not yet reconciled, report the rest.

        One activity for the whole run, not one per statement. The journal pool
        is shared across statements by design (spec §8.1): a transaction claimed
        by July's statement must not be offered to August's, and that is only
        true if they are matched together. Splitting the match per statement
        would also put a few thousand rows and their candidates through the
        workflow history on every hop.

        `post` false is a real run that writes nothing: it matches, it reports
        findings, and it produces the digest. That is the mode a person reads
        before letting a schedule touch the books.
        """
        from aegis.services import books, statement_findings, statement_match, statement_post
        from aegis.services.reconciled import mark_reconciled

        accounts = await _folder_config(self.db_pool)
        if not accounts or self.books_cfg is None:
            return {"status": "skipped", "reason": "not_configured", "statements": 0}

        statements = await load_statements(self.db_pool)
        rows = [r for s in statements for r in s.rows]
        if not rows:
            return {"status": "ok", "statements": 0, "posted": 0, "promoted": 0, "findings": {}}

        declared = await books.declared_accounts(self.books_cfg)
        rules = books.load_rules(self.books_cfg.path / "rules" / "accounts.yaml")
        # Without these every foreign-currency candidate reports itself
        # unrateable and §8.5 silently matches nothing. The rates exist; a
        # caller that forgets to pass them gets a clean-looking run with a
        # whole class of rows missing from it.
        rates = books.latest_prices(self.books_cfg)

        run = statement_match.match_statements(
            rows,
            await statement_match.load_candidates(
                self.db_pool,
                start=min(r.occurred_on for r in rows),
                end=max(r.occurred_on for r in rows),
                declared=declared,
            ),
            declared=declared,
            entity_for_instrument=_entity_map(accounts),
            rates=rates,
        )
        outcomes = {o.row_id: o for o in run.outcomes}

        scope = _Scope(since=date.fromisoformat(since) if since else None)
        done = {
            r["statement_id"]
            for r in await self.db_pool.fetch(
                "SELECT statement_id FROM finance.statements WHERE reconciled_at IS NOT NULL"
            )
        }
        posted = promoted = 0
        results: list[dict] = []
        for statement in statements:
            if statement.statement_id in done:
                continue
            if not scope.covers(statement):
                results.append({"statement": statement.statement_id, "status": "out_of_scope"})
                continue
            if not post:
                continue
            account = accounts.get(statement.instrument) or {}
            try:
                result = await statement_post.post_statement(
                    statement,
                    {rid: o for rid, o in outcomes.items() if o.statement_id == statement.statement_id},
                    self.books_cfg,
                    entity=account.get("post_entity") or "personal",
                    rules=rules,
                    liability=books.instrument_account(
                        statement.instrument, declared
                    ).startswith("liabilities:"),
                )
            except books.BooksCheckError as exc:
                # §15.4: a closing-balance mismatch is an arrival-time event,
                # not a sweep finding. The whole statement reverted; the
                # problem stays open until a later statement for this account
                # reconciles.
                await statement_findings.record_closing_balance(
                    self.db_pool,
                    statement_id=statement.statement_id,
                    instrument=statement.instrument,
                    reason=str(exc),
                )
                results.append({"statement": statement.statement_id, "status": "reverted"})
                continue

            posted += len(result.posted)
            promoted += len(result.promoted)
            results.append(
                {
                    "statement": statement.statement_id,
                    "status": "posted",
                    "posted": len(result.posted),
                    "promoted": len(result.promoted),
                    "balance_checked": result.balance_checked,
                }
            )
            # Only a statement the bank's own figures agreed with may move the
            # watermark. A card with no printed balances proves itself through
            # §6.2's arithmetic instead, which the parser already ran — but a
            # statement that could not be checked at all must not close a
            # period against later email evidence.
            if result.balance_checked:
                await mark_reconciled(
                    self.db_pool,
                    statement.instrument,
                    statement.period_end,
                    statement_id=statement.statement_id,
                )
                await self.db_pool.execute(
                    "UPDATE finance.statements SET reconciled_at = now() WHERE statement_id = $1",
                    statement.statement_id,
                )
            await statement_findings.clear_closing_balance(
                self.db_pool,
                statement_id=statement.statement_id,
                instrument=statement.instrument,
            )

        # Coverage is swept in the SAME call as the rest of the instrument
        # classes, and it has to be: `statement_missing` is one of them, so a
        # sweep that produced no coverage findings would resolve every open
        # "no statement arrived" problem for the reason that it never looked.
        period = _last_month(date.today())
        findings = statement_findings.match_findings(
            run
        ) + statement_findings.missing_statement_findings(
            accounts,
            {s.instrument for s in statements if s.period_end.strftime("%Y-%m") == period},
            period=period,
        )
        swept = await statement_findings.sweep(
            self.db_pool,
            findings,
            # Say which kinds this tick evaluated. A kind left out arrives as an
            # empty findings list, and an empty list is what resolves every open
            # problem of that kind — so a match-only run that stayed quiet would
            # report every locked statement as fixed. The `statement` kind is
            # intake's to sweep, not this activity's.
            kinds=(statement_findings.INSTRUMENT, statement_findings.CURRENCY),
        )
        return {
            "status": "ok",
            "statements": len(statements),
            "posted": posted,
            "promoted": promoted,
            "results": results,
            "findings": {
                k: {
                    "fresh": len(v.get("fresh") or []),
                    "resolved": len(v.get("resolved") or []),
                }
                for k, v in swept.items()
            },
            "digest": statement_findings.monthly_digest(run),
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


def _last_month(today: date) -> str:
    """The calendar month before `today`, as `YYYY-MM`.

    Coverage asks "did a statement arrive for last month?" rather than "for
    this month": a statement for the current month has not been sent yet, so
    asking about it would report every account as missing, every day, until
    the month ended.
    """
    first = today.replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def _entity_map(accounts: Mapping[str, Any]) -> dict[str, Any]:
    """instrument -> the entities pass 2b may consider for it (spec §8.1)."""
    return {k: v.get("entities") or [] for k, v in accounts.items()}


@dataclass
class _Scope:
    """Which statements this run is allowed to post.

    A Drive folder holds every statement the bank ever sent, and spec §2 scopes
    the lane to the period the books cover. The folder currently holds an
    FY2024-25 Axis statement of 1,619 rows that predates the books by two
    years: posting it is a decision about what the ledger is FOR, not something
    a schedule should do because the file happened to be there.
    """

    since: date | None = None

    def covers(self, statement: Any) -> bool:
        return self.since is None or statement.period_start >= self.since
