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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import structlog
from temporalio import activity

logger = structlog.get_logger()

#: The `mailbox` a statement-posted index row carries. Not a real mailbox —
#: the row came from a bank statement, not from mail — but `journal_index`
#: requires one, and a distinct value is what makes these rows findable and
#: keeps them out of any per-mailbox count.
_STATEMENT_MAILBOX = "statement"

#: The settings row holding `{"accounts": {instrument: {folder_id, entities,
#: post_entity}}}`. One row rather than one setting per account: the folder ids
#: and the entity scope are read together on every run and drift apart if they
#: are edited apart.
FOLDER_SETTING = "integration:statement_folders"

#: The `YYYY-MM` the reconciliation digest was last produced for. A month, not
#: a timestamp: what the cadence needs to know is which report has already been
#: written, and that survives a tick missed on the 1st.
DIGEST_SETTING = "statement_digest_month"


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
        from aegis.services import journal_index as ji
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
        # #470: keep the verdict, before anything acts on it — and only for a
        # statement not yet reconciled. Once one is, its record is frozen: the
        # matcher then sees the lane's own `stmt/` blocks as candidates, and its
        # verdict describes that, not the row. A row matched to an email on the
        # tick that posted came back the next day as ambiguous between that
        # email and its neighbour's own block. A record only — `record_outcomes`
        # says why nothing may read it back.
        await record_outcomes(
            self.db_pool, (o for o in outcomes.values() if o.statement_id not in done)
        )
        posted = promoted = 0
        results: list[dict] = []
        for statement in statements:
            if statement.statement_id in done:
                continue
            if not scope.covers(statement):
                results.append({"statement": statement.statement_id, "status": "out_of_scope"})
                continue
            account = accounts.get(statement.instrument) or {}
            try:
                result = await statement_post.post_statement(
                    statement,
                    # The WHOLE outcome set, not this statement's slice.
                    # `plan` looks rows up by id and ignores the rest, but
                    # `_far_block` reads the FAR row's outcome to find the block
                    # its email counterpart promoted — and the far row belongs
                    # to another statement by definition. Filtering here made
                    # that lookup always miss in production, so a transfer whose
                    # posting side was email-matched reverted the counterpart's
                    # statement for ever, while the test passed because it hands
                    # over the full dict.
                    outcomes,
                    self.books_cfg,
                    entity=account.get("post_entity") or "personal",
                    rules=rules,
                    # Every row of every OTHER account, so §8.4 can see both
                    # sides of a transfer. Passing this statement's own rows
                    # back would let a row pair with itself.
                    peer_rows=[r for r in rows if r.instrument != statement.instrument],
                    liability=books.instrument_account(
                        statement.instrument, declared
                    ).startswith("liabilities:"),
                    # `post` false still runs the plan, so a person can read
                    # what the lane WOULD write before letting it write.
                    dry_run=not post,
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
            except Exception as exc:  # noqa: BLE001 — one statement, not the run
                # Anything else — a commodity hledger cannot price, a parser
                # surprise — is this statement's problem and must not take the
                # rest of the run with it. The flow is NO_RETRY, so an escaping
                # exception costs the findings sweep, the digest and every
                # statement after this one in the loop, daily.
                logger.warning(
                    "statement_post_failed",
                    statement=statement.statement_id,
                    error=f"{type(exc).__name__}: {exc}"[:300],
                )
                results.append(
                    {
                        "statement": statement.statement_id,
                        "status": "failed",
                        "reason": f"{type(exc).__name__}: {exc}"[:300],
                    }
                )
                continue

            if not post:
                results.append(
                    {
                        "statement": statement.statement_id,
                        "status": "would_post",
                        "posted": len(result.posted),
                        "promoted": len(result.promoted),
                        "skipped": [list(s) for s in result.skipped],
                    }
                )
                continue

            # §7 and §9.2: a statement-posted block gets an index row like any
            # other. Without one, three things break, and the first is a real
            # double count: a vendor receipt arriving after the statement posted
            # the payment finds no counterpart — `journal_index.find_match`
            # requires `journal_file IS NOT NULL` — so it posts a SECOND block
            # for money the books already hold, and §9.3 cannot see it because
            # both blocks are inside the period. The other two: the row is never
            # a matcher candidate again, so its account's "N unmatched rows"
            # count can never fall; and `ledger_reclassify` reads the index, so
            # a statement-posted `expenses:unknown` cannot be moved by chat.
            for msgid, event, journal_file in result.indexed:
                await ji.upsert(
                    self.db_pool,
                    msgid,
                    _STATEMENT_MAILBOX,
                    event,
                    journal_file=journal_file,
                    declared=declared,
                )
            # `posted_at` marks the rows this post wrote a block for (032) — the
            # digest's marker, never the idempotency ledger, which is the
            # `stmt/<row_id>` msgid inside the block and survives a crash
            # between the journal commit and this stamp. Only where NULL: a
            # row's first stamp stands.
            if result.posted:
                await self.db_pool.execute(
                    "UPDATE finance.statement_rows SET posted_at = now() "
                    "WHERE row_id = ANY($1::text[]) AND posted_at IS NULL",
                    [m.removeprefix(f"{statement_post.MSGID_PREFIX}/") for m in result.posted],
                )

            # Promotion just rewrote every `assets:unknown` posting in these
            # blocks to this statement's account, so the journal now names the
            # account and the index still says NULL (#408). The journal is the
            # record; leaving the index disagreeing with it is how "which
            # account paid for this?" gets a wrong answer from the cheap side.
            await ji.name_instrument(
                self.db_pool, result.promoted, statement.instrument, declared=declared
            )

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

        # Row findings speak only for statements the lane can still act on, and
        # the digest reads the same run. Two kinds of statement are not that,
        # and between them they held every row of the lane's open money
        # problems on 2026-09-11:
        #
        # * OUT OF SCOPE — the loop above never posts one, so its rows can never
        #   close. 2,486 unmatched rows on axis-9640 and axis-cc-1313 sat in
        #   statements starting before `since`, as tasks nothing could finish.
        # * RECONCILED — it passed §9.3, so the bank's own printed totals agree
        #   with the books. An unmatched or ambiguous row left in one is the
        #   matcher not seeing its own posted entry, or an own-account transfer
        #   indexed under the other account — not money missing from the books.
        #
        # Reconciled is read AFTER the loop, so a statement this tick reconciled
        # counts: its rows were unmatched a moment before the loop posted them.
        # Only the per-statement parts are narrowed; unscoped instruments and
        # missing rates are about accounts and currencies and stay whole, and
        # coverage below reads every statement.
        reconciled = {
            r["statement_id"]
            for r in await self.db_pool.fetch(
                "SELECT statement_id FROM finance.statements WHERE reconciled_at IS NOT NULL"
            )
        }
        open_ids = {
            s.statement_id
            for s in statements
            if scope.covers(s) and s.statement_id not in reconciled
        }
        run = replace(
            run,
            summaries=tuple(s for s in run.summaries if s.statement_id in open_ids),
            outcomes=tuple(o for o in run.outcomes if o.statement_id in open_ids),
        )

        # Coverage rides the SAME call as the matcher's classes, because
        # `statement_missing` is an instrument class. That alone is not enough:
        # what decides whether its open problems may be resolved is whether
        # coverage LOOKED, and inside its grace window it does not (#491).
        # `reconcile_findings` resolves every open problem of a class it is
        # handed and did not find, so handing it `statement_missing` on days
        # 1-8 closed every "no statement arrived" task because nothing was
        # checked, and a statement really missing came back as a new task on
        # the 9th, every month. None means coverage did not look, and naming the
        # class `unevaluated` leaves its problems alone while the rest of the
        # kind still resolves.
        coverage = _coverage_findings(statements, statement_findings, today=date.today())
        findings = statement_findings.match_findings(run) + (coverage or [])
        swept = await statement_findings.sweep(
            self.db_pool,
            findings,
            # Say which kinds this tick evaluated. A kind left out arrives as an
            # empty findings list, and an empty list is what resolves every open
            # problem of that kind — so a match-only run that stayed quiet would
            # report every locked statement as fixed. The `statement` kind is
            # intake's to sweep, not this activity's.
            kinds=(statement_findings.INSTRUMENT, statement_findings.CURRENCY),
            unevaluated={statement_findings.STATEMENT_MISSING} if coverage is None else (),
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
            "digest": await _due_digest(
                self.db_pool, statement_findings, run, today=date.today()
            ),
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


async def record_outcomes(pool: Any, outcomes: Iterable[Any]) -> int:
    """Store what the matcher decided about each row (#470). Returns rows written.

    `match_statements` returns a verdict per row — the journal transaction it
    matched, or the candidates it could not choose between — and the tick
    handed that to the poster and dropped it. `matched_msgid` was NULL on all
    2,800 rows, so "which email did this bank row match?" had no answer once
    the run ended, which is the question a person asks when a statement
    reverts.

    **A record, never an input.** Nothing reads these columns back into a match
    or a post: every tick re-decides every row from the journal as it stands,
    and a stored match read back as evidence would outlive the evidence for it.

    `skip_reason` is deliberately not written. `candidates` with no
    `matched_msgid` already records "ambiguous", and spec §8.4 makes the poster
    the column's owner (`transfer_counterpart`): a matcher writing its own
    verdict there would wipe the poster's value every tick, through the guard
    below, the day that writer lands.

    Only a row whose verdict CHANGED is written: the tick runs daily over every
    stored row, and rewriting ~2,800 of them to the values they already hold is
    table bloat for nothing — the same reasoning as `store_rows`' `WHERE`.

    One statement, carried as one jsonb document rather than `unnest` over
    arrays: asyncpg reads a list of lists as a multi-dimensional array, and
    each row's candidates are a list of their own length, so they cannot ride
    a `jsonb[]` parameter. The pool's codec encodes the document itself — never
    pre-dump it.
    """
    batch = [
        {
            "row_id": o.row_id,
            "matched_msgid": o.msgid if o.matched else None,
            "candidates": list(o.candidates) or None,
        }
        for o in outcomes
    ]
    if not batch:
        return 0
    result = await pool.execute(
        """
        UPDATE finance.statement_rows AS sr
           SET matched_msgid = v.matched_msgid,
               candidates    = v.candidates
          FROM jsonb_to_recordset($1::jsonb)
               AS v(row_id text, matched_msgid text, candidates jsonb)
         WHERE sr.row_id = v.row_id
           AND (sr.matched_msgid IS DISTINCT FROM v.matched_msgid
             OR sr.candidates    IS DISTINCT FROM v.candidates)
        """,
        batch,
    )
    written = int(result.split()[-1])
    if written:
        logger.info("statement_outcomes_recorded", rows=written)
    return written


#: How long after a month ends before a missing statement is a finding rather
#: than a statement that has not arrived yet. Both banks send within days of the
#: period closing, so asking on the 1st would flip every account to missing and
#: resolve it again a few days later — a Todoist task per account per month
#: that says nothing except that the calendar turned over.
_COVERAGE_GRACE_DAYS = 8

#: How long an account may stay quiet before its next statement is overdue. A
#: month plus slack for a bank that bills on the 31st — the question is "has
#: this account had time to send another?", and 35 days is the shortest span
#: for which the answer is yes whatever day of the month it bills on.
_CYCLE_DAYS = 35


def _coverage_findings(statements, findings_mod, *, today: date) -> list[dict] | None:
    """§15.4's `statement_missing`: which accounts stopped sending.

    Two rules, and the first is the one that matters. **An account that has
    NEVER sent a statement is not a coverage failure.** `axis-cc-1747`,
    `icici-143` and `nkgsb-843` are declared and configured with a Drive folder
    and have never produced a file — asking "did one arrive last month?" of
    those opens three problems and three Todoist tasks that no statement can
    ever resolve. Coverage is about a bank that has stopped, which is only
    answerable for a bank that had started.

    Second, wait out `_COVERAGE_GRACE_DAYS` after the month closes before
    asking, so the answer is "it never came" rather than "it is the 2nd".
    Until then this returns None — "did not look" — never `[]`, which means
    "looked, and nothing is missing".
    """
    month_start, month_end = _last_month(today)
    first_of_month = today.replace(day=1)
    if (today - first_of_month).days < _COVERAGE_GRACE_DAYS:
        # Still inside the grace window, so coverage was not evaluated, and None
        # says so. `[]` here resolved every open `statement_missing` problem on
        # the 1st of the month for the reason that nothing was checked (#491).
        return None
    ever = {s.instrument for s in statements}
    # A statement covers a month when its period spans the MIDDLE of it. A
    # bank's billing period is its own business: HDFC bills the 5th to the 4th,
    # so `2026-08-05..09-04` covers all but four days of August while ENDING in
    # September. Comparing end months called that account missing every month it
    # reported on time (#463) — two live false tasks no statement could resolve.
    # Plain overlap would fix that and break the other direction, because July's
    # `07-05..08-04` overlaps August too and would cover for an August that
    # never came. The midpoint is what makes exactly one statement per cycle
    # answer for each month, whatever day the bank bills on.
    midpoint = month_start.replace(day=15)
    covered = {
        s.instrument
        for s in statements
        if s.period_start <= midpoint <= s.period_end
    }
    # And do not ask an account about a month whose statement its own cycle has
    # not had time to produce. HDFC bills the 12th to the 11th, so August's
    # statement is only ISSUED on 11 September — the calendar grace above opens
    # the question on the 9th, and the account is reported missing for two days
    # every month and then resolves. `_COVERAGE_GRACE_DAYS` waits out the
    # month; this waits out the ACCOUNT, which is the thing actually sending.
    latest: dict[str, date] = {}
    for s in statements:
        if s.period_end > latest.get(s.instrument, date.min):
            latest[s.instrument] = s.period_end
    fresh = {i for i, end in latest.items() if (today - end).days < _CYCLE_DAYS}
    return findings_mod.missing_statement_findings(
        ever, covered | fresh, period=month_start.strftime("%Y-%m")
    )


async def _due_digest(pool: Any, findings_mod, run, *, today: date) -> str:
    """The digest, at most once a month (#464).

    §15.4 keeps `monthly_digest` as the periodic READ on how the lane is doing —
    the findings are the actionable surface. The flow ticks daily and produced
    it on every tick: a long per-account list whose counts barely move between
    days, which teaches the reader to skip exactly the report they should read.

    The marker is the month the digest was produced FOR, not the day it went
    out. A tick missed on the 1st still produces the month's report on the 2nd,
    and two ticks on the same day produce one. A schedule running `silent`
    still spends the month's marker on a digest the flow drops — that is what
    silent asks for, and the alternative is teaching this activity about a
    delivery decision that belongs to the flow.
    """
    month = today.strftime("%Y-%m")
    if await pool.fetchval("SELECT value FROM settings WHERE key = $1", DIGEST_SETTING) == month:
        return ""
    digest = findings_mod.monthly_digest(run, period=month)
    await pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
        DIGEST_SETTING,
        # The bare string: the pool's jsonb codec applies `json.dumps` itself,
        # and pre-dumping it here lands a double-encoded scalar that never
        # compares equal to the month on the next tick.
        month,
    )
    return digest


def _last_month(today: date) -> tuple[date, date]:
    """The first and last day of the calendar month before `today`.

    Coverage asks "did a statement arrive for last month?" rather than "for
    this month": a statement for the current month has not been sent yet, so
    asking about it would report every account as missing, every day, until
    the month ended.
    """
    last = today.replace(day=1) - timedelta(days=1)
    return last.replace(day=1), last


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
