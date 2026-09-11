"""The money lane's reconciliation findings, as problem-hub events (spec §15).

§11 of the statement spec named two bespoke alerts and assumed machinery that
does not exist for this lane. The hub already owns all of it: identity,
per-event idempotency, one open problem per thing, the Todoist projection —
and recovery, which is the half every hand-rolled watchdog gets wrong. So a
finding here is a dict in `hub_watch.reconcile_findings`'s shape, and this
module is only the mapping from what `match_statements` and `intake_folder`
already return onto that shape.

Two distinctions decide everything else, and the first draft of §15.4 left
both ambiguous.

**Arrival-time versus swept.** `reconcile_findings` resolves every problem
whose class it was handed and whose subject is not among this tick's findings.
A closing-balance mismatch happens when a statement is posted, not when a
sweep runs, so the sweep never "finds" one — list `closing_balance` among the
swept classes and every mismatch is resolved on the very next tick. It is
ingested directly (:func:`record_closing_balance`), resolved directly
(:func:`clear_closing_balance`), and :data:`SWEEP_CLASSES` is built by
*excluding* :data:`ARRIVAL_CLASSES`, so a later arrival-time class cannot be
swept into a sweep list by hand.

**One subject kind per call.** `reconcile_findings` takes exactly one, so the
file-scoped classes need their own call. Mixed into the instrument-scoped one,
each set would resolve the other: neither ever appears among the other's
findings. :func:`sweep` makes one call per kind straight out of
:data:`SWEEP_CLASSES`, which is the only place that mapping lives — nothing
re-derives which class belongs to which call.

**No grouping, because the correlation key already grouped.** A finding whose
subject is the instrument is one problem per account by construction:
`unmatched_rows:instrument:axis-cc-1313` IS "214 unmatched rows on
axis-cc-1313", and the 215th row joins it instead of opening another task.
`hub_group` would undo exactly that, so `hub_group.NON_GROUPABLE_SOURCES`
keeps this source out of the sweep's clustering.

Nothing here writes a Todoist comment. Projection goes through
`hub_project.project`, whose comments carry the `Workflow run: problem-hub`
footer that clarify's loop guard and `work_sessions.is_user_note` exclude. A
projector without one re-reads its own comments as human signal — the
self-grading loop that made 39 of 39 "user corrections" fake.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import asyncpg
import structlog

from aegis.services import hub_project
from aegis.services.hub import Event, IngestResult, correlation_key, ingest_event
from aegis.services.hub_watch import reconcile_findings
from aegis.services.statement_intake import IntakeReport
from aegis.services.statement_match import MatchRun, bank_of
from aegis.services.statements import StatementLocked

logger = structlog.get_logger()

#: The hub source every event in this module carries. In `hub.SOURCES`.
SOURCE = "money"

# --- the classes (§15.3) ----------------------------------------------------

#: The books and the bank disagree over one statement's period. Arrival-time.
CLOSING_BALANCE = "closing_balance"
#: Statement rows on one account that no journal transaction matched.
UNMATCHED_ROWS = "unmatched_rows"
#: Rows on one account with more than one candidate, so none was chosen (§9.4).
AMBIGUOUS_ROW = "ambiguous_row"
#: A declared account with no statement for the period.
STATEMENT_MISSING = "statement_missing"
#: A file in the Drive folder nothing could parse.
UNPARSED_FILE = "unparsed_file"
#: An account with no declared entity, so matching pass 2b cannot run for it.
UNSCOPED_INSTRUMENT = "unscoped_instrument"
#: A currency on a journal candidate with no rate in `prices.journal`.
MISSING_RATE = "missing_rate"
#: A locked statement none of the derived passwords opened.
PASSWORD_FAILED = "password_failed"

#: Subject kinds. `instrument` is §15.3's account-scoped kind and `statement`
#: its file-scoped one. `currency` is a third because a missing rate's subject
#: really is a currency: calling it an instrument would put a lie in the
#: correlation key and in every status block that prints it, and `sweep`
#: derives its calls from the mapping below rather than from a hand-kept pair
#: of lists, so a third kind costs nothing and cannot be mixed up.
INSTRUMENT = "instrument"
STATEMENT = "statement"
CURRENCY = "currency"

#: Every class, and what kind of thing it is about. One table: the sweep lists,
#: the finding builder and the arrival-time path all read it.
SUBJECT_KIND_FOR_CLASS: dict[str, str] = {
    AMBIGUOUS_ROW: INSTRUMENT,
    CLOSING_BALANCE: STATEMENT,
    MISSING_RATE: CURRENCY,
    PASSWORD_FAILED: STATEMENT,
    STATEMENT_MISSING: INSTRUMENT,
    UNMATCHED_ROWS: INSTRUMENT,
    UNPARSED_FILE: STATEMENT,
    UNSCOPED_INSTRUMENT: INSTRUMENT,
}
CLASSES = frozenset(SUBJECT_KIND_FOR_CLASS)

#: Classes that happen on arrival rather than on a sweep. A sweep resolves the
#: complement of what it finds, and it can never find one of these, so a class
#: in here that also appeared in a sweep's `classes` would be resolved on the
#: very next tick. See the module docstring.
ARRIVAL_CLASSES = frozenset({CLOSING_BALANCE})

#: Subject kind -> the classes one `reconcile_findings` call may resolve.
#: Derived by excluding `ARRIVAL_CLASSES`, so the two rules above are enforced
#: by construction rather than by remembering them.
SWEEP_CLASSES: dict[str, tuple[str, ...]] = {
    kind: tuple(
        sorted(
            k
            for k, v in SUBJECT_KIND_FOR_CLASS.items()
            if v == kind and k not in ARRIVAL_CLASSES
        )
    )
    for kind in sorted(set(SUBJECT_KIND_FOR_CLASS.values()))
}
SWEEP_CLASSES = {kind: classes for kind, classes in SWEEP_CLASSES.items() if classes}

#: The three sweep class lists by name, for a caller that wants one call.
INSTRUMENT_CLASSES = SWEEP_CLASSES[INSTRUMENT]
STATEMENT_CLASSES = SWEEP_CLASSES[STATEMENT]
CURRENCY_CLASSES = SWEEP_CLASSES[CURRENCY]

DEFAULT_SEVERITY = "warning"
#: A mismatch means the books are wrong about an account, which is what the
#: whole lane exists to prevent; a locked statement means the lane is blind for
#: that account until a person acts. Everything else is work to do, not an
#: emergency.
_SEVERITY = {CLOSING_BALANCE: "critical", PASSWORD_FAILED: "error"}

#: How many examples ride in a payload. The problem is the count; the examples
#: are there so a reader can start somewhere.
_EXAMPLE_CAP = 10
_TITLE_CAP = 500


def severity_for(klass: str) -> str:
    return _SEVERITY.get(klass, DEFAULT_SEVERITY)


def finding(
    klass: str,
    subject: str,
    title: str,
    *,
    severity: str = "",
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One finding in `reconcile_findings`'s shape, with its subject kind.

    ``payload`` is an explicit argument rather than ``**kwargs`` because a
    finding's payload names files and accounts: a key called ``title`` would
    collide with this function's own parameter, which is a TypeError at the one
    call site that happens to carry one.

    Raises on an unknown class. That is a wiring mistake, and the hub's own
    `validate_event` takes the same line: a class nobody declared would key a
    problem no sweep ever resolves.
    """
    if klass not in CLASSES:
        raise ValueError(f"unknown money finding class {klass!r}")
    return {
        "klass": klass,
        "subject": str(subject),
        "subject_kind": SUBJECT_KIND_FOR_CLASS[klass],
        "title": str(title)[:_TITLE_CAP],
        "severity": severity or severity_for(klass),
        "payload": {k: v for k, v in (payload or {}).items() if v is not None},
    }


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


# --- builders ---------------------------------------------------------------


def match_findings(run: MatchRun) -> list[dict[str, Any]]:
    """Everything one match run found wrong, as findings.

    Counts are **per instrument, not per statement**. A run usually covers
    several months of one account, and a per-statement count would make the
    number in the title mean "July", which falls when July is fixed and rises
    when August is imported — a count that moves for reasons unrelated to the
    work. Per instrument it is the account's whole backlog and only the rules
    getting better makes it fall.
    """
    unmatched: dict[str, int] = {}
    ambiguous: dict[str, int] = {}
    examples: dict[str, list[dict[str, Any]]] = {}
    statements: dict[str, list[str]] = {}

    for summary in run.summaries:
        instrument = (summary.instrument or "").strip()
        if not instrument:
            # Fail open and say so: a statement whose instrument did not
            # survive parsing is still a real statement, and swallowing it
            # silently is how a whole account goes quiet.
            logger.warning(
                "money_finding_skipped",
                reason="no_instrument",
                statement=summary.statement_id,
            )
            continue
        statements.setdefault(instrument, []).append(summary.statement_id)
        unmatched[instrument] = unmatched.get(instrument, 0) + summary.unmatched
        ambiguous[instrument] = ambiguous.get(instrument, 0) + len(summary.ambiguous)
        for row_id, candidates in summary.ambiguous:
            examples.setdefault(instrument, []).append(
                {"row": row_id, "candidates": list(candidates)[:_EXAMPLE_CAP]}
            )

    out: list[dict[str, Any]] = []
    for instrument in sorted(statements):
        rows = unmatched.get(instrument, 0)
        if rows:
            out.append(
                finding(
                    UNMATCHED_ROWS,
                    instrument,
                    f"{rows} unmatched {_plural(rows, 'row', 'rows')} on {instrument}",
                    payload={
                        "rows": rows,
                        "statements": sorted(statements[instrument])[:_EXAMPLE_CAP],
                    },
                )
            )
        rows = ambiguous.get(instrument, 0)
        if rows:
            # §15.6: report-only for now. The eventual resolution is a person
            # picking a candidate through an InteractionFlow card, but a card
            # per row at 959 rows is worse than the digest it replaces, so the
            # card waits until the residue is a handful a month.
            out.append(
                finding(
                    AMBIGUOUS_ROW,
                    instrument,
                    f"{rows} ambiguous {_plural(rows, 'row', 'rows')} on {instrument}",
                    payload={
                        "rows": rows,
                        "examples": examples.get(instrument, [])[:_EXAMPLE_CAP],
                    },
                )
            )

    for instrument in run.unscoped_instruments:
        if not instrument:
            continue
        out.append(
            finding(
                UNSCOPED_INSTRUMENT,
                instrument,
                f"No entity declared for {instrument}, so matching pass 2b cannot run",
                payload={"instrument": instrument},
            )
        )
    for currency in run.missing_rates:
        if not currency:
            continue
        out.append(
            finding(
                MISSING_RATE,
                currency,
                f"No {currency} rate in prices.journal, so its rows cannot be matched",
                payload={"currency": currency},
            )
        )
    return out


def intake_findings(report: IntakeReport) -> list[dict[str, Any]]:
    """Every file the folder walk could not turn into rows, as findings.

    A locked file is its own class: the fix is a password component, not a
    parser. `intake_folder` records the exception's own class name in the
    reason, so the split reads the vocabulary from `statements` rather than
    re-inventing a marker string.
    """
    locked = f"{StatementLocked.__name__}:"
    out: list[dict[str, Any]] = []
    for outcome in report.failures:
        subject = "/".join(p for p in (outcome.folder, outcome.title) if p)
        if not subject:
            logger.warning("money_finding_skipped", reason="no_file_subject", file=outcome.file_id)
            continue
        name = outcome.title or outcome.file_id or "a file"
        where = f" in {outcome.folder}" if outcome.folder else ""
        if outcome.reason.startswith(locked):
            out.append(
                finding(
                    PASSWORD_FAILED,
                    subject,
                    f"{name}{where} is locked and no derived password opened it",
                    payload={
                        "file_id": outcome.file_id,
                        "folder": outcome.folder,
                        "file_title": outcome.title,
                        "reason": outcome.reason,
                    },
                )
            )
            continue
        out.append(
            finding(
                UNPARSED_FILE,
                subject,
                f"{name}{where} did not parse ({outcome.status})",
                payload={
                    "file_id": outcome.file_id,
                    "folder": outcome.folder,
                    "file_title": outcome.title,
                    "status": outcome.status,
                    "reason": outcome.reason,
                },
            )
        )
    return out


def missing_statement_findings(
    declared: Collection[str], covered: Collection[str], *, period: str
) -> list[dict[str, Any]]:
    """One finding per declared account with no statement covering ``period``.

    Coverage is a finding like any other, which is the point of putting it
    here: it stops being one the day the statement lands, with no close path
    to write.
    """
    have = {str(c).strip() for c in covered if str(c).strip()}
    return [
        finding(
            STATEMENT_MISSING,
            instrument,
            f"No statement for {instrument} covering {period}",
            payload={"period": period, "instrument": instrument},
        )
        for instrument in sorted({str(d).strip() for d in declared if str(d).strip()} - have)
    ]


# --- the sweep --------------------------------------------------------------


def _key_for(f: Mapping[str, Any]) -> str:
    """This finding's correlation key, computed by the hub's own function —
    a producer never spells a key out for itself."""
    return correlation_key(
        Event(
            source=SOURCE,
            external_id="",
            kind="occurrence",
            title="",
            subject=str(f.get("subject") or ""),
            subject_kind=str(f.get("subject_kind") or ""),
            klass=str(f.get("klass") or ""),
        )
    )


async def refresh_titles(
    pool: asyncpg.Pool, findings: Sequence[Mapping[str, Any]]
) -> int:
    """Make each live problem's title say this tick's count. Returns how many
    changed.

    The count IS the title — "214 unmatched rows on axis-cc-1313" — and
    `ingest_event` writes a title only when it CREATES a problem. Without this
    the task would say 214 for ever while the payload said 3, and the whole
    reason one problem per account is bearable is that the number falls as the
    rules improve. `hub_project.project` renames the task on its next run
    because the stored title no longer matches the one it last projected.

    Scoped to the keys this lane just produced, so it can only ever rewrite its
    own problems.
    """
    changed = 0
    for f in findings:
        key, title = _key_for(f), str(f.get("title") or "")
        if not key or not title:
            continue
        result = await pool.execute(
            "UPDATE problems SET title = $2 "
            "WHERE correlation_key = $1 AND closed_at IS NULL AND title <> $2",
            key,
            title[:_TITLE_CAP],
        )
        changed += 0 if result.endswith(" 0") else 1
    return changed


async def sweep(
    pool: asyncpg.Pool,
    findings: Sequence[Mapping[str, Any]],
    *,
    kinds: Collection[str] = (),
    unevaluated: Collection[str] = (),
    now: datetime | None = None,
    project: bool = True,
) -> dict[str, dict[str, Any]]:
    """Hand ``findings`` to the hub, one `reconcile_findings` call per subject
    kind. Returns each call's result, keyed by kind.

    ``kinds`` says which kinds this tick actually evaluated, and defaults to
    all of them. **Pass it when you ran only part of the lane.** A kind you did
    not evaluate arrives as an empty findings list, and an empty findings list
    is what resolves every open problem of that kind — so a matcher-only run
    that forgot to say so would report every locked statement as fixed.

    ``unevaluated`` is the same promise for a CLASS inside an evaluated kind:
    it is left out of the classes its kind's call may resolve, so its open
    problems stay as they are. Coverage inside its grace window is the case
    (#491) — it shares the instrument kind with the matcher's classes, so
    leaving out the kind would stop those resolving too.

    Findings of an arrival-time class are refused rather than quietly dropped:
    they belong to :func:`record_closing_balance`, and sweeping one would
    resolve it on the next tick.
    """
    now = now or datetime.now(UTC)
    wanted = tuple(kinds) if kinds else tuple(SWEEP_CLASSES)
    by_kind: dict[str, list[dict[str, Any]]] = {kind: [] for kind in wanted}
    for f in findings:
        klass = str(f.get("klass") or "")
        if klass in ARRIVAL_CLASSES:
            raise ValueError(f"{klass} is an arrival-time class and must not be swept")
        kind = SUBJECT_KIND_FOR_CLASS.get(klass) or str(f.get("subject_kind") or "")
        if kind not in by_kind:
            # Fail open, and say what was withheld: a finding of a kind this
            # tick did not evaluate is still real, it just has no sweep to
            # resolve it against.
            logger.warning(
                "money_finding_unswept",
                klass=klass,
                kind=kind,
                subject=str(f.get("subject") or "")[:80],
            )
            continue
        by_kind[kind].append(dict(f))

    out: dict[str, dict[str, Any]] = {}
    for kind in wanted:
        classes = SWEEP_CLASSES.get(kind)
        if not classes:
            logger.warning("money_sweep_unknown_kind", kind=kind)
            continue
        out[kind] = await reconcile_findings(
            pool,
            source=SOURCE,
            subject_kind=kind,
            classes=[c for c in classes if c not in unevaluated],
            findings=by_kind[kind],
            now=now,
            project=project,
        )
    await refresh_titles(pool, findings)
    return out


# --- the arrival-time class -------------------------------------------------


async def _project_quietly(pool: asyncpg.Pool, problem_id: str, now: datetime) -> None:
    """Give the problem its task. Never raises: a projection failure must not
    lose the record of a mismatch, and the hub sweep retries projection."""
    try:
        await hub_project.project(pool, problem_id, now=now)
    except Exception as exc:  # noqa: BLE001 — the hub sweep retries projection
        logger.warning("money_project_failed", problem_id=problem_id, error=str(exc)[:200])


async def record_closing_balance(
    pool: asyncpg.Pool,
    *,
    statement_id: str,
    instrument: str,
    reason: str,
    now: datetime | None = None,
    project: bool = True,
) -> IngestResult:
    """The books and the bank disagree over one statement (§9.3).

    Ingested directly and never swept — see the module docstring on
    arrival-time classes. ``reason`` is `movement_disagreement`'s sentence,
    which already names both figures and the difference.
    """
    now = now or datetime.now(UTC)
    result = await ingest_event(
        pool,
        Event(
            source=SOURCE,
            external_id=f"{SOURCE}:{CLOSING_BALANCE}:{statement_id}@{now.isoformat()}",
            kind="occurrence",
            title=f"The books disagree with {statement_id}"[:_TITLE_CAP],
            subject=statement_id,
            subject_kind=STATEMENT,
            klass=CLOSING_BALANCE,
            severity=severity_for(CLOSING_BALANCE),
            payload={
                "statement_id": statement_id,
                "instrument": instrument,
                "reason": reason[:1000],
            },
            occurred_at=now,
        ),
        now=now,
    )
    if project and result.investigate and result.problem_id:
        await _project_quietly(pool, result.problem_id, now)
    return result


async def clear_closing_balance(
    pool: asyncpg.Pool,
    *,
    statement_id: str,
    instrument: str = "",
    now: datetime | None = None,
    project: bool = True,
) -> IngestResult:
    """This statement reconciled, so its mismatch is over.

    Keyed on the STATEMENT, not the account. August passing says nothing about
    July: July's period is still unreconciled, and resolving it because a later
    month agreed would close the only record that it never did. A mismatch
    ends when the statement that raised it is posted again and agrees.

    A no-op when there is no open mismatch — the hub answers ``ignored``.
    """
    now = now or datetime.now(UTC)
    result = await ingest_event(
        pool,
        Event(
            source=SOURCE,
            external_id=f"{SOURCE}:{CLOSING_BALANCE}:{statement_id}@{now.isoformat()}@resolved",
            kind="resolved",
            title=f"{statement_id} reconciles with the books",
            subject=statement_id,
            subject_kind=STATEMENT,
            klass=CLOSING_BALANCE,
            payload={"statement_id": statement_id, "instrument": instrument},
            occurred_at=now,
        ),
        now=now,
    )
    if project and result.action == "resolved" and result.problem_id:
        await _project_quietly(pool, result.problem_id, now)
    return result


# --- the digest (§15.4 "kept: the monthly digest") --------------------------


def monthly_digest(
    run: MatchRun,
    intake: IntakeReport | None = None,
    *,
    period: str = "",
    statements: Mapping[str, int] | None = None,
) -> str:
    """The month's reconciliation as a report. Pure, and it alerts nobody.

    The hub owns alerting now, so this exists for the other question — how well
    is the lane working — which a task per finding answers badly. Match rate
    per bank says whether the rules are improving; the per-account unmatched
    counts say where the work is.

    ``statements`` counts what ``run`` was narrowed FROM — ``reconciled``,
    ``out_of_scope``, ``open`` — because the reconcile activity hands over only
    the statements the lane can still act on. Without it the goal state, every
    statement in scope reconciled, reads as a digest that saw nothing.
    """
    head = f"Reconciliation digest{f' — {period}' if period else ''}"
    lines = [head, "=" * len(head)]
    if statements is not None:
        total = sum(statements.values())
        lines.append(
            f"{total} {_plural(total, 'statement', 'statements')}: "
            f"{statements.get('reconciled', 0)} reconciled, "
            f"{statements.get('out_of_scope', 0)} out of scope, "
            f"{statements.get('open', 0)} open"
        )

    banks: dict[str, list[int]] = {}
    per_account: list[tuple[str, int, int]] = []
    ambiguous: list[tuple[str, str, tuple[str, ...]]] = []
    for summary in run.summaries:
        instrument = (summary.instrument or "").strip() or "unknown"
        totals = banks.setdefault(bank_of(instrument), [0, 0])
        totals[0] += summary.matched_total
        totals[1] += summary.rows
        per_account.append((summary.statement_id, summary.rows, summary.unmatched))
        for row_id, candidates in summary.ambiguous:
            ambiguous.append((instrument, row_id, candidates))

    lines.append("")
    lines.append("Match rate")
    if banks:
        for bank in sorted(banks):
            matched, rows = banks[bank]
            pct = (matched / rows * 100) if rows else 0.0
            lines.append(f"  {bank}: {matched}/{rows} rows matched ({pct:.1f}%)")
    elif statements is None:
        lines.append("  no statements in this run")
    elif not statements.get("open"):
        lines.append("  all in-scope statements reconcile with the books")
    else:
        # Open statements, but nothing of theirs left to match: every row was
        # posted this tick, or there were none. Not reconciled either way.
        lines.append("  no row of the open statements is left to match")

    unmatched = [a for a in per_account if a[2]]
    if unmatched:
        lines.append("")
        lines.append("Unmatched rows")
        for statement_id, rows, count in sorted(unmatched):
            lines.append(f"  {statement_id}: {count} of {rows}")

    if ambiguous:
        lines.append("")
        lines.append(f"Ambiguous rows ({len(ambiguous)}) — nothing posted, one candidate each")
        for instrument, row_id, candidates in ambiguous[:_EXAMPLE_CAP]:
            shown = ", ".join(candidates[:4]) + ("…" if len(candidates) > 4 else "")
            lines.append(f"  {instrument} {row_id}: {shown}")
        if len(ambiguous) > _EXAMPLE_CAP:
            lines.append(f"  …and {len(ambiguous) - _EXAMPLE_CAP} more")

    if run.unscoped_instruments:
        lines.append("")
        lines.append("No entity declared (pass 2b did not run)")
        lines.extend(f"  {i}" for i in run.unscoped_instruments)
    if run.missing_rates:
        lines.append("")
        lines.append("No rate in prices.journal")
        lines.extend(f"  {c}" for c in run.missing_rates)

    failures = list(intake.failures) if intake is not None else []
    if failures:
        lines.append("")
        lines.append(f"Files not imported ({len(failures)})")
        for outcome in failures[:_EXAMPLE_CAP]:
            where = f"{outcome.folder}/" if outcome.folder else ""
            lines.append(f"  {where}{outcome.title}: {outcome.status} — {outcome.reason}")
        if len(failures) > _EXAMPLE_CAP:
            lines.append(f"  …and {len(failures) - _EXAMPLE_CAP} more")
    return "\n".join(lines)
