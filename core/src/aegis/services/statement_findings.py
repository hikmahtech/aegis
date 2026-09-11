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
from aegis.services.hub import Event, IngestResult, correlation_key, ingest_event, slug
from aegis.services.hub_watch import reconcile_findings
from aegis.services.statement_intake import IntakeReport
from aegis.services.statement_match import AMBIGUOUS, MatchRun, RowOutcome, bank_of
from aegis.services.statements import StatementLocked, StatementRow

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
#: How many ids `row_ids` may carry. Every one rides in every occurrence, daily.
#: ponytail: past the cap the list is left out and the finding is never taken as
#: acknowledged, so an account that far behind is reported every run. Store the
#: ids once per problem if one ever gets there.
_ROW_ID_CAP = 2000
#: The row classes: their items are rows, so an acknowledgement is per row.
_ROW_CLASSES = frozenset({UNMATCHED_ROWS, AMBIGUOUS_ROW})
#: The last line of every swept money task. A person completing the task is
#: what `_acknowledged` reads.
_ACK_LINE = (
    "Ticking this off tells Maou you have dealt with it. "
    "It stays quiet until something new turns up."
)


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


# --- what a task says -------------------------------------------------------
#
# `hub_project.project` builds a new task's body from the occurrence's
# `description`. Without one a money task was the status block alone: it never
# said which rows, or what to do.


def _describe(*parts: str) -> str:
    """A task body: what is wrong, what to do, and how to make it stay quiet."""
    return "\n\n".join([*(p for p in parts if p), _ACK_LINE])


def _row_line(outcome: RowOutcome, rows: Mapping[str, StatementRow] | None) -> str:
    """`2026-08-03 · out · 1234.00 · UPI/AMAZON PAY…`, what a person looks for
    on the statement. A row the caller did not hand over is named by its id."""
    row = (rows or {}).get(outcome.row_id)
    if row is None:
        return f"{outcome.occurred_on.isoformat()} · row {outcome.row_id}"
    narration = " ".join(str(row.narration or "").split())[:60]
    return f"{row.occurred_on.isoformat()} · {row.direction} · {row.amount} · {narration}"


def _row_list(
    outcomes: Sequence[RowOutcome],
    rows: Mapping[str, StatementRow] | None,
    *,
    candidates: bool = False,
) -> str:
    """The first `_EXAMPLE_CAP` rows by date, then how many more there are."""
    shown = sorted(outcomes, key=lambda o: (o.occurred_on, o.row_id))
    lines = []
    for o in shown[:_EXAMPLE_CAP]:
        line = _row_line(o, rows)
        if candidates:
            n = len(o.candidates)
            line += f" · {n} {_plural(n, 'candidate', 'candidates')}"
        lines.append(line)
    if len(shown) > _EXAMPLE_CAP:
        lines.append(f"and {len(shown) - _EXAMPLE_CAP} more")
    return "\n".join(lines)


def _row_ids(outcomes: Sequence[RowOutcome], klass: str, instrument: str) -> list[str] | None:
    """Every row id, for `_acknowledged`, or None past `_ROW_ID_CAP` — and
    `finding` leaves a None out of the payload."""
    ids = sorted({o.row_id for o in outcomes})
    if len(ids) <= _ROW_ID_CAP:
        return ids
    logger.warning("money_finding_row_ids_capped", klass=klass, instrument=instrument, rows=len(ids))
    return None


# --- builders ---------------------------------------------------------------


def match_findings(
    run: MatchRun, *, rows: Mapping[str, StatementRow] | None = None
) -> list[dict[str, Any]]:
    """Everything one match run found wrong, as findings.

    Counts are **per instrument, not per statement**. A run usually covers
    several months of one account, and a per-statement count would make the
    number in the title mean "July", which falls when July is fixed and rises
    when August is imported — a count that moves for reasons unrelated to the
    work. Per instrument it is the account's whole backlog and only the rules
    getting better makes it fall.

    ``rows`` is every statement row by id. An outcome carries a row's id and
    date but not its amount or narration, and those are what let a person find
    the row on the statement; without them a row is named by its id.
    """
    unmatched: dict[str, int] = {}
    ambiguous: dict[str, int] = {}
    examples: dict[str, list[dict[str, Any]]] = {}
    statements: dict[str, list[str]] = {}
    # The rows behind each count, per instrument, by the same filters as the
    # summary's counts (`statement_match.summarise`), so the ids and the number
    # in the title always agree.
    instrument_of = {s.statement_id: (s.instrument or "").strip() for s in run.summaries}
    unmatched_rows: dict[str, list[RowOutcome]] = {}
    ambiguous_rows: dict[str, list[RowOutcome]] = {}
    for o in run.outcomes:
        if o.skip_reason == AMBIGUOUS:
            ambiguous_rows.setdefault(instrument_of.get(o.statement_id, ""), []).append(o)
        elif not o.matched and o.skip_reason is None:
            unmatched_rows.setdefault(instrument_of.get(o.statement_id, ""), []).append(o)

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
        count = unmatched.get(instrument, 0)
        if count:
            listed = unmatched_rows.get(instrument, [])
            out.append(
                finding(
                    UNMATCHED_ROWS,
                    instrument,
                    f"{count} unmatched {_plural(count, 'row', 'rows')} on {instrument}",
                    payload={
                        "rows": count,
                        "statements": sorted(statements[instrument])[:_EXAMPLE_CAP],
                        "row_ids": _row_ids(listed, UNMATCHED_ROWS, instrument),
                        "description": _describe(
                            f"{count} {_plural(count, 'row', 'rows')} on {instrument} "
                            f"{_plural(count, 'matches', 'match')} nothing in the books:\n"
                            + _row_list(listed, rows),
                            "Post each one you recognise, for example by asking Maou in chat. "
                            "If the books already hold one under another date or amount, fix "
                            "that entry instead.",
                        ),
                    },
                )
            )
        count = ambiguous.get(instrument, 0)
        if count:
            # §15.6: report-only for now. The eventual resolution is a person
            # picking a candidate through an InteractionFlow card, but a card
            # per row at 959 rows is worse than the digest it replaces, so the
            # card waits until the residue is a handful a month.
            listed = ambiguous_rows.get(instrument, [])
            out.append(
                finding(
                    AMBIGUOUS_ROW,
                    instrument,
                    f"{count} ambiguous {_plural(count, 'row', 'rows')} on {instrument}",
                    payload={
                        "rows": count,
                        "examples": examples.get(instrument, [])[:_EXAMPLE_CAP],
                        # Every id: the examples stop at ten.
                        "row_ids": _row_ids(listed, AMBIGUOUS_ROW, instrument),
                        "description": _describe(
                            f"{count} {_plural(count, 'row', 'rows')} on {instrument} "
                            f"{_plural(count, 'matches', 'match')} more than one transaction "
                            "in the books, so none was chosen:\n"
                            + _row_list(listed, rows, candidates=True),
                            "Check whether the books hold the same payment twice, and remove "
                            "the copy if they do.",
                        ),
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
                payload={
                    "instrument": instrument,
                    "description": _describe(
                        f"No entity is declared for {instrument}, so matching cannot use the "
                        "payments that do not say which account paid.",
                        f"Add the entities {instrument} pays for to its entry in the "
                        "integration:statement_folders setting.",
                    ),
                },
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
                payload={
                    "currency": currency,
                    "description": _describe(
                        f"prices.journal has no {currency} rate, so rows charged in {currency} "
                        "cannot be matched.",
                        f"Add a {currency} rate to prices.journal in the books.",
                    ),
                },
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
                        "description": _describe(
                            f"{name}{where} is locked, and none of the passwords AEGIS tried "
                            "opened it.",
                            "Replace it with an unlocked copy.",
                        ),
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
                    "description": _describe(
                        f"{name}{where} did not parse ({outcome.status}): {outcome.reason[:200]}",
                        "Check it is a statement AEGIS knows how to read. Replace it with a "
                        "clean copy, or move it out of the folder.",
                    ),
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
            payload={
                "period": period,
                "instrument": instrument,
                "description": _describe(
                    f"No statement for {instrument} covers {period}.",
                    f"Drop the {instrument} statement covering {period} into its Drive folder.",
                ),
            },
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


def _items(klass: str, subject: str, payload: Mapping[str, Any]) -> frozenset[str] | None:
    """What a finding is about, item by item: the rows of a row class, the
    period of a missing statement, and the subject alone for every other class.

    None when a row class carries no `row_ids` — past `_ROW_ID_CAP`, or an
    occurrence recorded before the key existed. With nothing to compare, such
    a finding is never taken as acknowledged.
    """
    if klass in _ROW_CLASSES:
        ids = payload.get("row_ids")
        return frozenset(str(i) for i in ids) if isinstance(ids, list) else None
    if klass == STATEMENT_MISSING:
        period = str(payload.get("period") or "")
        return frozenset({period}) if period else None
    return frozenset({slug(subject)})


async def _acknowledged(pool: asyncpg.Pool, f: Mapping[str, Any]) -> bool:
    """Whether a person has already dealt with everything this finding says.

    Completing a money task resolved its problem (`hub_project.
    reconcile_completed_tasks`), and the next statement run found the same
    rows and reopened the problem and the task — so ticking one off was
    pointless (prod 2026-09-11). Acknowledged means all three:

    * the newest problem for the finding's key is `resolved` or `closed`. A
      closed problem keeps its row, so the acknowledgement outlives the
      seven-day close that frees the key;
    * its latest resolve was a person's completion, told apart by the source
      `reconcile_completed_tasks` writes it with
      (`hub_project.TASK_COMPLETED_SOURCE`) rather than by the reason's
      wording, which a later edit could change;
    * every item in the finding was already in the problem's last occurrence
      before that completion.

    A finding with anything new is not acknowledged, and the hub does what it
    always did: reopens the problem inside the reopen window, or opens a fresh
    problem and task after it.
    """
    klass = str(f.get("klass") or "")
    subject = str(f.get("subject") or "")
    items = _items(klass, subject, f.get("payload") or {})
    key = _key_for(f)
    if items is None or not key:
        return False
    row = await pool.fetchrow(
        """
        WITH p AS (
            SELECT id, status FROM problems WHERE correlation_key = $1
            ORDER BY (closed_at IS NULL) DESC, first_seen_at DESC LIMIT 1
        ), done AS (
            SELECT e.id, e.source, e.external_id FROM problem_events e JOIN p ON e.problem_id = p.id
            WHERE e.kind = 'state_change' AND e.payload->>'action' = 'resolve'
            ORDER BY e.id DESC LIMIT 1
        )
        SELECT p.status,
               done.source = 'hub' AND done.external_id LIKE $2 AS by_person,
               (SELECT o.payload FROM problem_events o
                 WHERE o.problem_id = p.id AND o.kind = 'occurrence' AND o.id < done.id
                 ORDER BY o.id DESC LIMIT 1) AS seen
        FROM p LEFT JOIN done ON true
        """,
        key,
        f"{hub_project.TASK_COMPLETED_SOURCE}:%",
    )
    if row is None or row["status"] not in ("resolved", "closed") or not row["by_person"]:
        return False
    seen = _items(klass, subject, row["seen"] or {})
    return seen is not None and items <= seen


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

    Findings of an arrival-time class are refused rather than quietly dropped:
    they belong to :func:`record_closing_balance`, and sweeping one would
    resolve it on the next tick.

    A finding a person has already acknowledged (:func:`_acknowledged`) is
    dropped before the hub sees it. Absent, it resolves nothing — its problem
    already is — and its title is left as the person ticked it off.
    """
    now = now or datetime.now(UTC)
    wanted = tuple(kinds) if kinds else tuple(SWEEP_CLASSES)
    by_kind: dict[str, list[dict[str, Any]]] = {kind: [] for kind in wanted}
    acknowledged: set[str] = set()
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
        if await _acknowledged(pool, f):
            acknowledged.add(_key_for(f))
            continue
        by_kind[kind].append(dict(f))
    if acknowledged:
        logger.info("money_findings_acknowledged", count=len(acknowledged))

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
            classes=list(classes),
            findings=by_kind[kind],
            now=now,
            project=project,
        )
    await refresh_titles(pool, [f for f in findings if _key_for(f) not in acknowledged])
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
    run: MatchRun, intake: IntakeReport | None = None, *, period: str = ""
) -> str:
    """The month's reconciliation as a report. Pure, and it alerts nobody.

    The hub owns alerting now, so this exists for the other question — how well
    is the lane working — which a task per finding answers badly. Match rate
    per bank says whether the rules are improving; the per-account unmatched
    counts say where the work is.
    """
    head = f"Reconciliation digest{f' — {period}' if period else ''}"
    lines = [head, "=" * len(head)]

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
    else:
        lines.append("  no statements in this run")

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
