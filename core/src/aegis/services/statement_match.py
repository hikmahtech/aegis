"""The statement matcher (spec `2026-09-07-statement-reconciliation-design.md` §8).

**Report only.** Nothing here writes the journal: no `books.post_event`, no
`books.rewrite_event`, no rule application. It reads `finance.journal_index`
and returns what it found, so the passes and the date window can be measured
against real data before step 5 posts anything.

The shape of §8.1, kept exactly:

| Pass | Key | Outcome |
|---|---|---|
| 1 (`ref`) | the UTR/RRN out of the narration against `journal_index.ref` | match |
| 2 (`window`) | instrument + direction + amount + date window, exactly one candidate | match |
| 2b (`window_no_instrument`) | as 2, over candidates with `instrument IS NULL`, scoped to the instrument's entity | match |
| 3 | more than one candidate in 2 or 2b | **no match — ambiguous** |

One statement row matches at most one journal transaction and each journal
transaction is claimed once; a claimed transaction leaves the pool. Direction is
never crossed, on any pass. Instruments are compared through
`books.canonical_instrument()` on **both** sides, because the live index carries
`card-1313`, `nkgsb-0843`, `nkgsb-8443` and `axis-1` for accounts the chart
spells differently.

**Determinism.** Pass order decides who claims what, so the order rows arrive in
must not: rows are sorted into a canonical order — `(occurred_on, direction,
amount, row_id)` — before any pass runs, each pass runs over every unresolved row
before the next pass starts, and a candidate list is sorted by msgid. Feed the
same rows in any order and the same transactions are claimed by the same rows.
Without that, a row that is ambiguous claims nothing and hands its candidate to
whichever unique row happens to be processed next — a different answer per run.

**The date window is `journal_index._MATCH_DAYS`**, the one window for the lane
(§8.2), and `window_days` exists to widen it for the measurement that decides
what that constant should be — not as a second constant.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from aegis.services import books, journal_index
from aegis.services.statements import StatementRow

#: Pass names, in the order they run. `PASS_REF` is §8.1's pass 1, `PASS_WINDOW`
#: pass 2 and `PASS_WINDOW_NO_INSTRUMENT` pass 2b.
PASS_REF = "ref"
PASS_WINDOW = "window"
PASS_WINDOW_NO_INSTRUMENT = "window_no_instrument"
PASSES = (PASS_REF, PASS_WINDOW, PASS_WINDOW_NO_INSTRUMENT)

#: `skip_reason` values this module writes. `AMBIGUOUS` is §9.4's: two or more
#: journal transactions carry this amount on this instrument in this window, one
#: of them IS this row, and choosing mis-attributes a payment silently.
AMBIGUOUS = "ambiguous"
DUPLICATE = "duplicate_row"

#: §8.5's foreign-currency band — the FX markup a card adds on top of the rate.
FX_BAND = Decimal("0.05")

#: Both banks in scope print rupees, and `finance.statement_rows` has no
#: currency column: a statement amount IS in this currency. Anything else on the
#: journal side is the §8.5 candidate class, matched through `books.latest_prices`.
STATEMENT_CURRENCY = "INR"


@dataclass(frozen=True)
class Candidate:
    """One journal transaction, reduced to what the matcher compares.

    A candidate must have a journal block (`journal_file IS NOT NULL`). That is
    not a detail: the bank alert and the vendor receipt for one payment are two
    index rows and only the first to arrive posts a block, so without the filter
    one payment offers two candidates and every such row reads as ambiguous.
    Step 4 also promotes a row by rewriting its block, which a blockless row
    does not have.
    """

    msgid: str
    entity: str
    direction: str
    amount: Decimal
    currency: str
    occurred_on: date
    instrument: str | None = None
    ref: str | None = None
    #: Which extractor produced this row. Pass 1 reads it for one decision and
    #: nothing else — see `_ref_is_located`.
    parser: str | None = None


@dataclass(frozen=True)
class RowOutcome:
    """What the matcher decided about one statement row."""

    row_id: str
    statement_id: str
    instrument: str
    occurred_on: date
    matched_pass: str | None = None
    msgid: str | None = None
    delta_days: int | None = None
    foreign: bool = False
    candidates: tuple[str, ...] = ()
    skip_reason: str | None = None

    @property
    def matched(self) -> bool:
        return self.matched_pass is not None


@dataclass(frozen=True)
class StatementSummary:
    """The per-statement run summary (§8, deliverable 4)."""

    statement_id: str
    instrument: str
    rows: int
    matched: Mapping[str, int]
    foreign: int
    ambiguous: tuple[tuple[str, tuple[str, ...]], ...]
    unmatched: int
    duplicates: int

    @property
    def matched_total(self) -> int:
        return sum(self.matched.values())


@dataclass(frozen=True)
class MatchRun:
    outcomes: tuple[RowOutcome, ...]
    summaries: tuple[StatementSummary, ...]
    claimed: Mapping[str, str]
    #: Instruments pass 2b could not run for, because no entity was declared for
    #: them. Reported rather than guessed: matching NULL-instrument candidates
    #: entity-blind is how a hikmah payment lands in `personal/2026.journal`.
    unscoped_instruments: tuple[str, ...] = ()
    #: Currencies seen on a candidate with no rate in `prices.journal`. A silent
    #: zero here reads as "no foreign-currency transactions", which is wrong.
    missing_rates: tuple[str, ...] = ()


def bank_of(instrument: str) -> str:
    """`axis-cc-1313` -> `axis`. The bank is the instrument's first segment."""
    return instrument.split("-", 1)[0] if instrument else "unknown"


# Everything that is not a letter or a digit, which is the difference between
# the same reference as three systems print it: a bank writes
# "UTR 5261-1234-5678", its statement narration writes "526112345678", and the
# extractor copies whichever it was shown.
_REF_NOISE = re.compile(r"[^0-9A-Za-z]+")


def _norm_ref(ref: str | None) -> str | None:
    """One reference reduced to what makes it the same reference.

    This runs on BOTH sides of the pass-1 join, and it has to: `llm._ref_from_body`
    verifies a model's answer against the mail on alphanumerics but stores the
    string verbatim, so a reference the model copied with the bank's own spacing
    ("UTR 5261-1234-5678") never equalled the statement's bare digits under the
    old `strip().upper()`. Pass 1 then found nothing, silently — the join simply
    missed, which is indistinguishable from "this row has no counterpart", so the
    lift #433 was built to buy would have been invisibly smaller with no error
    anywhere to say why.

    Collisions this could newly create are references that differ only in
    punctuation, which are the same reference.
    """
    text = _REF_NOISE.sub("", ref or "").upper()
    return text or None


def _canonical(instrument: str | None, declared: Collection[str]) -> str | None:
    return books.canonical_instrument(instrument, declared) if instrument else None


def _rate_for(currency: str, rates: Mapping[str, Decimal]) -> Decimal | None:
    """The rupee price of one unit of `currency`, or None.

    `books.latest_prices` keys on the journal SYMBOL (`$`, `£`), which is what
    `prices.journal` prints, while `journal_index.currency` holds the ISO code.
    Both spellings are accepted so a caller can hand over either map.
    """
    symbol = books._SYMBOL.get(currency)
    if symbol and symbol in rates:
        return rates[symbol]
    return rates.get(currency)


def _ref_is_located(candidate: Candidate) -> bool:
    """True when this candidate's reference was READ OUT OF the mail rather than
    lifted from a known position in it.

    A deterministic parser takes the reference from a fixed slot in a bank's own
    alert — `_HDFC_UPI` captures the digits after "UPI transaction reference
    no.:" — so the number it returns is, by construction, this payment's
    reference. That is why pass 1 was designed to match on the reference alone:
    neither instrument nor date window is required, which is the whole reason it
    runs first.

    An extracted reference is a different object with the same name. `llm`'s
    `_ref_from_body` can only verify that the characters appear somewhere in the
    mail; it cannot tell whose payment they name. A merchant receipt echoing a
    previous order's bank reference, or a summary listing several RRNs, yields a
    real reference belonging to a DIFFERENT payment — and since only 4 of 13
    deterministic parsers set `ref`, the genuine block usually has none to
    compete with, so the wrong block would be pass 1's sole candidate and win
    outright. Pass 1 outranks every later pass, so step 5 would promote the
    wrong block to `*` and post the real row again as a duplicate.

    Direction already blocks the commonest shape (a refund quoting the original
    UTR is `in` against an `out`). The amount is the cheap witness for the rest.

    Scoped rather than blanket deliberately: requiring corroboration everywhere
    would break the deterministic case, where a card auth and its settlement can
    legitimately differ by a tip and the reference is the thing that knows they
    are one payment.
    """
    return (candidate.parser or "") == "llm"


def _amount_matches(
    amount: Decimal,
    candidate: Candidate,
    *,
    currency: str,
    rates: Mapping[str, Decimal],
    missing_rates: set[str],
) -> bool:
    """Does this candidate carry the row's amount — same currency, or §8.5's band?

    Amount equality never matches across currencies, so a `$4.00` journal block
    against a `₹338` statement row would be an unmatched row that posts again in
    rupees, and nothing downstream catches it: hledger's `=` assertion is per
    commodity, so an account can hold a permanent dollar balance beside a
    correct rupee one and still pass.
    """
    if candidate.currency == currency:
        return candidate.amount == amount
    if not candidate.currency:
        return False
    rate = _rate_for(candidate.currency, rates)
    if rate is None:
        missing_rates.add(candidate.currency)
        return False
    converted = candidate.amount * rate
    if converted <= 0:
        return False
    return abs(amount - converted) <= converted * FX_BAND


def _ordered(
    rows: Iterable[StatementRow], declared: Collection[str]
) -> list[tuple[StatementRow, str]]:
    """Canonical processing order — see the module docstring on determinism.

    `row_id` breaks the tie and is a content hash, so the order is total and
    identical on every run whatever order the caller collected the rows in.
    """
    pairs = [(row, _canonical(row.instrument, declared) or row.instrument) for row in rows]
    return sorted(pairs, key=lambda p: (p[0].occurred_on, p[0].direction, p[0].amount, p[0].row_id))


def _entity_set(value: str | Collection[str] | None) -> frozenset[str]:
    """The entities pass 2b will accept for one instrument.

    An account does NOT have one entity, which is what the single-string
    version assumed. Measured on production 2026-09-07: `axis-cc-1313` carries
    6 hikmah and 5 personal transactions, so declaring either one hid the other
    half of the card from pass 2b — the pass that exists precisely to reach the
    blockless rows an instrument-aware pass cannot see.

    A string still works and means a set of one, because most accounts really
    are single-entity and saying so is the honest declaration. An empty value
    is the same as no declaration at all: pass 2b does not run, and the
    instrument is reported in `unscoped_instruments` rather than matched
    entity-blind. That refusal is the important half — matching pass 2b without
    a scope is how a hikmah payment lands in `personal/2026.journal`.

    This is the MATCHING scope, and it is deliberately not the posting default.
    Spec §4.1 needs exactly one entity per account to choose a journal file,
    and that stays one value: a row must land in one book. Widening the set
    that may be *considered* is not licence to widen the one that is *written*.
    """
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset({value}) if value else frozenset()
    return frozenset(e for e in value if e)


def match_statements(
    rows: Sequence[StatementRow],
    candidates: Sequence[Candidate],
    *,
    declared: Collection[str] = (),
    entity_for_instrument: Mapping[str, str | Collection[str]] | None = None,
    rates: Mapping[str, Decimal] | None = None,
    window_days: int = journal_index._MATCH_DAYS,
    currency: str = STATEMENT_CURRENCY,
) -> MatchRun:
    """Run §8.1's passes over `rows` and report; write nothing.

    `rows` may span several statements — the journal pool is shared across them,
    which is the point: a transaction claimed by the July statement is not
    offered to the August one.
    """
    rates = rates or {}
    # An empty set needs no filtering out: `_pass_candidates` reads this with
    # `entities.get(instrument) or frozenset()`, so a declared-but-empty entry
    # and a missing one are already the same thing — pass 2b skipped, the
    # instrument reported.
    entities = {
        (_canonical(inst, declared) or inst): _entity_set(e)
        for inst, e in (entity_for_instrument or {}).items()
    }
    pool = [
        Candidate(
            msgid=c.msgid,
            entity=c.entity,
            direction=c.direction,
            amount=c.amount,
            currency=c.currency,
            occurred_on=c.occurred_on,
            instrument=_canonical(c.instrument, declared),
            ref=_norm_ref(c.ref),
            parser=c.parser,
        )
        for c in candidates
    ]
    claimed: dict[str, str] = {}
    missing_rates: set[str] = set()
    unscoped: set[str] = set()
    outcomes: list[RowOutcome] = []

    unresolved: list[tuple[StatementRow, str]] = []
    seen_ids: set[str] = set()
    for row, instrument in _ordered(rows, declared):
        if row.row_id in seen_ids:
            # §8.3: overlapping statements produce identical ids for the same
            # printed row, by design. Matching it twice would claim a second
            # journal transaction for one payment, and the second copy would
            # then read as a genuine unmatched row in the report.
            outcomes.append(
                RowOutcome(
                    row_id=row.row_id,
                    statement_id=row.statement_id,
                    instrument=instrument,
                    occurred_on=row.occurred_on,
                    skip_reason=DUPLICATE,
                )
            )
            continue
        seen_ids.add(row.row_id)
        unresolved.append((row, instrument))

    for pass_name in PASSES:
        still: list[tuple[StatementRow, str]] = []
        for row, instrument in unresolved:
            found = _pass_candidates(
                pass_name,
                row,
                instrument,
                pool,
                claimed=claimed,
                entities=entities,
                rates=rates,
                missing_rates=missing_rates,
                unscoped=unscoped,
                window_days=window_days,
                currency=currency,
            )
            if not found:
                still.append((row, instrument))
                continue
            if len(found) > 1:
                # §9.4 — one of these IS this row, so posting it would add a
                # third copy of money the balance already counts. An ambiguous
                # row is finished: it does not fall through to a weaker pass,
                # which could only widen the field it already cannot choose in.
                outcomes.append(
                    RowOutcome(
                        row_id=row.row_id,
                        statement_id=row.statement_id,
                        instrument=instrument,
                        occurred_on=row.occurred_on,
                        candidates=tuple(c.msgid for c in found),
                        skip_reason=AMBIGUOUS,
                    )
                )
                continue
            candidate = found[0]
            claimed[candidate.msgid] = row.row_id
            outcomes.append(
                RowOutcome(
                    row_id=row.row_id,
                    statement_id=row.statement_id,
                    instrument=instrument,
                    occurred_on=row.occurred_on,
                    matched_pass=pass_name,
                    msgid=candidate.msgid,
                    delta_days=(row.occurred_on - candidate.occurred_on).days,
                    foreign=candidate.currency != currency,
                )
            )
        unresolved = still

    for row, instrument in unresolved:
        outcomes.append(
            RowOutcome(
                row_id=row.row_id,
                statement_id=row.statement_id,
                instrument=instrument,
                occurred_on=row.occurred_on,
            )
        )

    ordered_outcomes = tuple(sorted(outcomes, key=lambda o: (o.statement_id, o.row_id)))
    return MatchRun(
        outcomes=ordered_outcomes,
        summaries=summarise(ordered_outcomes),
        claimed=dict(claimed),
        unscoped_instruments=tuple(sorted(unscoped)),
        missing_rates=tuple(sorted(missing_rates)),
    )


def _pass_candidates(
    pass_name: str,
    row: StatementRow,
    instrument: str,
    pool: Sequence[Candidate],
    *,
    claimed: Mapping[str, str],
    entities: Mapping[str, frozenset[str]],
    rates: Mapping[str, Decimal],
    missing_rates: set[str],
    unscoped: set[str],
    window_days: int,
    currency: str,
) -> list[Candidate]:
    """Every unclaimed candidate this row could be, under one pass's key.

    Sorted by msgid so an ambiguous row reports the same candidate list, and a
    single-candidate pass claims the same transaction, on every run.
    """
    row_ref = _norm_ref(row.ref)
    allowed_entities = entities.get(instrument) or frozenset()
    if pass_name == PASS_REF and row_ref is None:
        return []
    if pass_name == PASS_WINDOW_NO_INSTRUMENT and not allowed_entities:
        unscoped.add(instrument)
        return []
    found: list[Candidate] = []
    for candidate in pool:
        if candidate.msgid in claimed:
            continue
        # §8.1: never match across direction, on any pass.
        if candidate.direction != row.direction:
            continue
        if pass_name == PASS_REF:
            if candidate.ref != row_ref:
                continue
            # An EXTRACTED reference needs the amount to agree as well; a parsed
            # one does not. See `_ref_is_located` for why the two are different
            # objects despite the shared column.
            if _ref_is_located(candidate) and not _amount_matches(
                row.amount,
                candidate,
                currency=currency,
                rates=rates,
                missing_rates=missing_rates,
            ):
                continue
            found.append(candidate)
            continue
        if pass_name == PASS_WINDOW:
            if candidate.instrument != instrument:
                continue
        else:  # pass 2b — no instrument at all, scoped to the account's entity
            if candidate.instrument is not None:
                continue
            if candidate.entity not in allowed_entities:
                continue
        if abs((row.occurred_on - candidate.occurred_on).days) > window_days:
            continue
        if not _amount_matches(
            row.amount,
            candidate,
            currency=currency,
            rates=rates,
            missing_rates=missing_rates,
        ):
            continue
        found.append(candidate)
    return sorted(found, key=lambda c: c.msgid)


def summarise(outcomes: Iterable[RowOutcome]) -> tuple[StatementSummary, ...]:
    """One `StatementSummary` per statement, in statement id order."""
    grouped: dict[str, list[RowOutcome]] = {}
    for outcome in outcomes:
        grouped.setdefault(outcome.statement_id, []).append(outcome)
    summaries = []
    for statement_id, rows in sorted(grouped.items()):
        matched = dict.fromkeys(PASSES, 0)
        for outcome in rows:
            if outcome.matched_pass is not None:
                matched[outcome.matched_pass] += 1
        summaries.append(
            StatementSummary(
                statement_id=statement_id,
                instrument=rows[0].instrument,
                rows=len(rows),
                matched=matched,
                foreign=sum(1 for o in rows if o.matched and o.foreign),
                ambiguous=tuple(
                    (o.row_id, o.candidates) for o in rows if o.skip_reason == AMBIGUOUS
                ),
                unmatched=sum(1 for o in rows if not o.matched and o.skip_reason is None),
                duplicates=sum(1 for o in rows if o.skip_reason == DUPLICATE),
            )
        )
    return tuple(summaries)


def date_delta_report(outcomes: Iterable[RowOutcome]) -> dict[str, Counter[int]]:
    """§8.2's deliverable: `statement_date - journal_date` per bank.

    **Pass 2 and 2b only.** Pass 1 joins on a reference, which is exact and says
    nothing about the lag between a bank posting a transaction and the email
    that announced it — and `journal_index.ref` is filled only by the
    deterministic parsers, so a report built on pass 1 would describe a handful
    of rows while reading as evidence about all of them. This distribution is
    the only thing that may move `journal_index._MATCH_DAYS`.
    """
    report: dict[str, Counter[int]] = {}
    for outcome in outcomes:
        if outcome.matched_pass not in (PASS_WINDOW, PASS_WINDOW_NO_INSTRUMENT):
            continue
        if outcome.delta_days is None:
            continue
        report.setdefault(bank_of(outcome.instrument), Counter())[outcome.delta_days] += 1
    return report


def window_coverage(
    report: Mapping[str, Mapping[int, int]], days: int
) -> dict[str, tuple[int, int]]:
    """Per bank, `(matches within ±days, matches in total)` — what a window buys."""
    return {
        bank: (
            sum(n for delta, n in deltas.items() if abs(delta) <= days),
            sum(deltas.values()),
        )
        for bank, deltas in report.items()
    }


async def load_candidates(
    pool: Any,
    *,
    start: date,
    end: date,
    declared: Collection[str] = (),
    window_days: int = journal_index._MATCH_DAYS,
) -> tuple[Candidate, ...]:
    """The journal transactions a statement covering `start`..`end` could match.

    Read-only. `journal_file IS NOT NULL` is the load-bearing predicate — see
    `Candidate`. The window is widened by `window_days` on both ends so a
    transaction just outside the statement period is still offered.
    """
    records = await pool.fetch(
        """
        SELECT message_id, entity, direction, amount, currency, instrument, ref, occurred_on,
               parser
        FROM finance.journal_index
        WHERE kind = 'transaction' AND journal_file IS NOT NULL
          AND amount IS NOT NULL AND direction IS NOT NULL AND occurred_on IS NOT NULL
          AND occurred_on BETWEEN $1 AND $2
        ORDER BY message_id
        """,
        start - timedelta(days=window_days),
        end + timedelta(days=window_days),
    )
    return tuple(
        Candidate(
            msgid=r["message_id"],
            entity=r["entity"],
            direction=r["direction"],
            amount=r["amount"],
            currency=r["currency"] or "",
            occurred_on=r["occurred_on"],
            instrument=_canonical(r["instrument"], declared),
            ref=_norm_ref(r["ref"]),
            parser=r["parser"],
        )
        for r in records
    )
