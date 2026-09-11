"""Reconciliation findings on the problem hub (spec §15.3–§15.8).

Real test database. Every class name, status and outcome comes from the module
that defines it — this lane already shipped a green test suite that threw away
all 15 real statements, because the fixtures invented the same wrong constant
the code did.

The database is shared with every other test in this file, and a sweep resolves
the complement of its findings, so each test asserts about its OWN subjects
(uuid-suffixed) rather than about totals.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from aegis.services import hub_project, statements
from aegis.services import statement_findings as sf
from aegis.services.hub import close_problem, get_problem
from aegis.services.statement_intake import MISFILED, UNREADABLE, FileOutcome, IntakeReport
from aegis.services.statement_match import Candidate, MatchRun, match_statements
from aegis.services.statements import PARSED, StatementLocked

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
JULY = (date(2026, 7, 1), date(2026, 7, 31))
AUGUST = (date(2026, 8, 1), date(2026, 8, 31))


def _instrument() -> str:
    return f"zzacct-{uuid.uuid4().hex[:8]}"


def make_row(
    instrument: str,
    day: int,
    amount: str,
    *,
    period: tuple[date, date] = JULY,
    direction: str = "out",
    narration: str = "TEST ROW",
) -> statements.StatementRow:
    occurred = date(period[0].year, period[0].month, day)
    money = Decimal(amount)
    return statements.StatementRow(
        row_id=statements.row_id_for(
            instrument=instrument,
            occurred_on=occurred,
            direction=direction,
            amount=money,
            balance_after=None,
            occurrence_index=0,
            narration=narration,
        ),
        instrument=instrument,
        occurred_on=occurred,
        narration=narration,
        ref=None,
        direction=direction,
        amount=money,
        balance_after=None,
        statement_id=statements.statement_id_for(instrument, *period),
        file_sha256="fixture-sha",
    )


def make_candidate(
    msgid: str,
    instrument: str,
    day: int,
    amount: str,
    *,
    period: tuple[date, date] = JULY,
    direction: str = "out",
    currency: str = "INR",
) -> Candidate:
    return Candidate(
        msgid=msgid,
        entity="personal",
        direction=direction,
        amount=Decimal(amount),
        currency=currency,
        occurred_on=date(period[0].year, period[0].month, day),
        instrument=instrument,
    )


def run_match(rows, candidates=(), **kw):
    kw.setdefault("declared", ())
    return match_statements(list(rows), list(candidates), **kw)


# --- the vocabulary ---------------------------------------------------------


def test_an_arrival_class_is_never_in_a_sweep_list():
    """Trap 1. `reconcile_findings` resolves every problem of a class it was
    given that is not among this tick's findings, and it can never find a
    closing-balance mismatch — that happens when a statement is posted. In a
    sweep list, every mismatch would be resolved on the next tick."""
    assert sf.CLOSING_BALANCE in sf.ARRIVAL_CLASSES
    for kind, classes in sf.SWEEP_CLASSES.items():
        assert sf.CLOSING_BALANCE not in classes, kind
    assert sf.CLOSING_BALANCE not in sf.STATEMENT_CLASSES


def test_every_swept_class_belongs_to_exactly_one_call():
    """Trap 2. A class in two calls, or in a call whose subject kind is not its
    own, means one set of findings resolves another set that never appears in
    it."""
    seen: list[str] = []
    for kind, classes in sf.SWEEP_CLASSES.items():
        for klass in classes:
            assert sf.SUBJECT_KIND_FOR_CLASS[klass] == kind
            seen.append(klass)
    assert sorted(seen) == sorted(sf.CLASSES - sf.ARRIVAL_CLASSES)
    assert len(seen) == len(set(seen))


def test_finding_stamps_the_subject_kind_and_refuses_an_unknown_class():
    f = sf.finding(sf.UNMATCHED_ROWS, "axis-cc-1313", "214 unmatched rows", payload={"rows": 214})
    assert f["subject_kind"] == sf.INSTRUMENT
    assert f["severity"] == sf.DEFAULT_SEVERITY
    assert f["payload"] == {"rows": 214}
    assert sf.finding(sf.CLOSING_BALANCE, "s", "t")["severity"] == "critical"
    with pytest.raises(ValueError, match="unknown money finding class"):
        sf.finding("invented_class", "x", "t")


# --- the builders -----------------------------------------------------------


def test_unmatched_rows_is_one_finding_per_account_carrying_the_count():
    """The correlation key does the grouping: one problem per account, with the
    count in the title so it falls as the rules improve."""
    inst = _instrument()
    run = run_match([make_row(inst, 3, "100.00"), make_row(inst, 4, "200.00")])
    found = [f for f in sf.match_findings(run) if f["klass"] == sf.UNMATCHED_ROWS]
    assert len(found) == 1
    assert found[0]["subject"] == inst
    assert found[0]["title"] == f"2 unmatched rows on {inst}"
    assert found[0]["payload"]["rows"] == 2


def test_counts_aggregate_across_the_statements_of_one_account():
    """Two months of one account are one backlog. A per-statement count would
    move when a different month was imported."""
    inst = _instrument()
    run = run_match(
        [
            make_row(inst, 3, "100.00"),
            make_row(inst, 4, "200.00"),
            make_row(inst, 5, "300.00", period=AUGUST),
        ]
    )
    assert len(run.summaries) == 2
    found = [f for f in sf.match_findings(run) if f["klass"] == sf.UNMATCHED_ROWS]
    assert len(found) == 1
    assert found[0]["payload"]["rows"] == 3
    assert len(found[0]["payload"]["statements"]) == 2


def test_an_ambiguous_row_is_a_report_only_finding_on_its_account():
    inst = _instrument()
    row = make_row(inst, 3, "100.00")
    run = run_match(
        [row],
        [
            make_candidate("m1", inst, 3, "100.00"),
            make_candidate("m2", inst, 4, "100.00"),
        ],
    )
    found = [f for f in sf.match_findings(run) if f["klass"] == sf.AMBIGUOUS_ROW]
    assert len(found) == 1
    assert found[0]["subject_kind"] == sf.INSTRUMENT
    assert found[0]["payload"]["rows"] == 1
    assert found[0]["payload"]["examples"][0]["candidates"] == ["m1", "m2"]
    # Report only: nothing here opens an interaction card (§15.6).
    assert not hasattr(sf, "ambiguous_row_card")


def test_unscoped_instruments_and_missing_rates_become_findings():
    inst = _instrument()
    # No entity declared for the instrument, so pass 2b cannot run.
    run = run_match([make_row(inst, 3, "100.00")], [make_candidate("m1", None, 3, "100.00")])
    assert run.unscoped_instruments == (inst,)
    classes = {f["klass"]: f for f in sf.match_findings(run)}
    assert classes[sf.UNSCOPED_INSTRUMENT]["subject"] == inst

    # A dollar candidate with no rate in prices.journal.
    run = run_match(
        [make_row(inst, 3, "338.00")],
        [make_candidate("m2", inst, 3, "4.00", currency="USD")],
        entity_for_instrument={inst: "personal"},
    )
    assert run.missing_rates == ("USD",)
    rate = next(f for f in sf.match_findings(run) if f["klass"] == sf.MISSING_RATE)
    assert (rate["subject"], rate["subject_kind"]) == ("USD", sf.CURRENCY)


def test_intake_findings_split_a_locked_file_from_an_unparsed_one():
    """`intake_folder` records the exception's own class name, so the split
    reads its marker from `statements` rather than inventing one."""
    report = IntakeReport(
        outcomes=[
            FileOutcome(file_id="f1", title="ok.pdf", folder="axis-9640", status=PARSED),
            FileOutcome(
                file_id="f2", title="locked.pdf", folder="axis-9640", status=UNREADABLE,
                reason=f"{StatementLocked.__name__}: the statement could not be opened",
            ),
            FileOutcome(
                file_id="f3", title="odd.pdf", folder="hdfc-1225", status=UNREADABLE,
                reason="ValueError: no columns found",
            ),
            FileOutcome(
                file_id="f4", title="wrong.pdf", folder="hdfc-1225", status=MISFILED,
                reason="the file says axis-9640, the folder says hdfc-1225",
            ),
        ]
    )
    by_class = {f["klass"]: f for f in sf.intake_findings(report)}
    assert set(by_class) == {sf.PASSWORD_FAILED, sf.UNPARSED_FILE}
    assert by_class[sf.PASSWORD_FAILED]["subject"] == "axis-9640/locked.pdf"
    assert by_class[sf.PASSWORD_FAILED]["payload"]["file_title"] == "locked.pdf"
    assert by_class[sf.PASSWORD_FAILED]["severity"] == "error"
    assert all(f["subject_kind"] == sf.STATEMENT for f in sf.intake_findings(report))
    # The parsed file is not a finding, and both non-locked failures are.
    assert len(sf.intake_findings(report)) == 3


def test_missing_statement_findings_name_only_the_uncovered_accounts():
    out = sf.missing_statement_findings(
        ["axis-9640", "hdfc-1225", "nkgsb-843"], ["hdfc-1225"], period="2026-08"
    )
    assert [f["subject"] for f in out] == ["axis-9640", "nkgsb-843"]
    assert out[0]["klass"] == sf.STATEMENT_MISSING
    assert out[0]["payload"]["period"] == "2026-08"


# --- what a money task says (the description) -------------------------------

ACK = (
    "Ticking this off tells Maou you have dealt with it. "
    "It stays quiet until something new turns up."
)


def test_an_unmatched_rows_task_lists_the_rows_and_says_what_to_do():
    """A money task body used to be the status block alone: it never said which
    rows, or what to do. The run's outcomes carry no amount or narration, so the
    caller hands the rows over."""
    inst = _instrument()
    rows = [make_row(inst, d, f"{d}00.00", narration=f"UPI/PAYEE {d}") for d in range(1, 13)]
    run = run_match(rows)
    found = next(
        f for f in sf.match_findings(run, rows={r.row_id: r for r in rows})
        if f["klass"] == sf.UNMATCHED_ROWS
    )
    text = found["payload"]["description"]
    assert "2026-07-01 · out · 100.00 · UPI/PAYEE 1\n" in text
    assert text.count(" · out · ") == 10
    assert "and 2 more" in text
    assert text.endswith(ACK)
    # Every row, not the ten shown: an acknowledgement is checked against all of them.
    assert sorted(found["payload"]["row_ids"]) == sorted(r.row_id for r in rows)
    # The title is untouched.
    assert found["title"] == f"12 unmatched rows on {inst}"

    # Without the rows, a row is still named, by its date and id.
    bare = next(f for f in sf.match_findings(run) if f["klass"] == sf.UNMATCHED_ROWS)
    assert f"2026-07-01 · row {rows[0].row_id}" in bare["payload"]["description"]


def test_an_ambiguous_row_task_names_each_row_and_its_candidates():
    """The examples in the payload stop at ten; `row_ids` holds every one."""
    inst = _instrument()
    row = make_row(inst, 3, "100.00", narration="CARD PURCHASE")
    run = run_match(
        [row], [make_candidate("m1", inst, 3, "100.00"), make_candidate("m2", inst, 4, "100.00")]
    )
    found = next(
        f for f in sf.match_findings(run, rows={row.row_id: row})
        if f["klass"] == sf.AMBIGUOUS_ROW
    )
    assert "2026-07-03 · out · 100.00 · CARD PURCHASE · 2 candidates" in found["payload"]["description"]
    assert found["payload"]["row_ids"] == [row.row_id]


def test_a_missing_statement_task_says_where_the_file_goes():
    out = sf.missing_statement_findings(["axis-9640"], [], period="2026-08")
    assert (
        "Drop the axis-9640 statement covering 2026-08 into its Drive folder."
        in out[0]["payload"]["description"]
    )


def test_every_swept_finding_says_how_to_make_it_stay_quiet():
    """Completing any swept money task acknowledges it, so every one says so."""
    inst = _instrument()
    run = run_match(
        [make_row(inst, 3, "100.00"), make_row(inst, 9, "338.00"), make_row(inst, 20, "500.00")],
        [
            # No entity declared, so pass 2b cannot look at these: unscoped.
            make_candidate("m1", None, 3, "100.00"),
            # A dollar candidate with no rate: missing_rate.
            make_candidate("m3", inst, 9, "4.00", currency="USD"),
            # Two candidates for one row: ambiguous.
            make_candidate("m4", inst, 20, "500.00"),
            make_candidate("m5", inst, 21, "500.00"),
        ],
    )
    report = IntakeReport(
        outcomes=[
            FileOutcome(
                file_id="f2", title="locked.pdf", folder="axis-9640", status=UNREADABLE,
                reason=f"{StatementLocked.__name__}: the statement could not be opened",
            ),
            FileOutcome(
                file_id="f3", title="odd.pdf", folder="hdfc-1225", status=UNREADABLE,
                reason="ValueError: no columns found",
            ),
        ]
    )
    found = (
        sf.match_findings(run)
        + sf.intake_findings(report)
        + sf.missing_statement_findings([inst], [], period="2026-08")
    )
    assert {f["klass"] for f in found} == sf.CLASSES - sf.ARRIVAL_CLASSES
    for f in found:
        assert f["payload"]["description"].endswith(ACK), f["klass"]


def test_row_ids_are_left_out_over_the_cap(monkeypatch):
    """Past the cap the list is left out, and the finding is then never taken
    as acknowledged — it is reported, as before."""
    monkeypatch.setattr(sf, "_ROW_ID_CAP", 2)
    inst = _instrument()
    rows = [make_row(inst, d, "100.00") for d in (3, 4, 5)]
    found = next(f for f in sf.match_findings(run_match(rows)) if f["klass"] == sf.UNMATCHED_ROWS)
    assert "row_ids" not in found["payload"]
    assert found["payload"]["rows"] == 3
    two = next(
        f for f in sf.match_findings(run_match(rows[:2])) if f["klass"] == sf.UNMATCHED_ROWS
    )
    assert len(two["payload"]["row_ids"]) == 2


# --- the sweep, and the two traps -------------------------------------------


async def _sweep(pool, findings, *, now=NOW, **kw):
    return await sf.sweep(pool, findings, now=now, project=False, **kw)


async def test_a_closing_balance_problem_survives_a_sweep(db_pool):
    """Trap 1, on a real database. The mismatch is ingested directly; a sweep
    of the classes it does not belong to must leave it open."""
    inst, other = _instrument(), _instrument()
    statement_id = statements.statement_id_for(inst, *JULY)
    result = await sf.record_closing_balance(
        db_pool,
        statement_id=statement_id,
        instrument=inst,
        reason="the bank moved 100, the books moved 90 — difference -10",
        now=NOW,
        project=False,
    )
    assert result.action == "created"
    problem = await get_problem(db_pool, result.problem_id)
    assert (problem["class"], problem["subject_kind"]) == (sf.CLOSING_BALANCE, sf.STATEMENT)
    assert problem["severity"] == "critical"
    # Its task says both figures, and that ticking it off will not keep it away.
    assert await db_pool.fetchval(
        "SELECT payload->>'description' FROM problem_events "
        "WHERE problem_id = $1::uuid AND kind = 'occurrence'",
        result.problem_id,
    ) == (
        "the bank moved 100, the books moved 90 — difference -10\n\n"
        "This task comes back until the statement reconciles with the books."
    )

    # A full sweep tick that produces no closing-balance finding, on both the
    # instrument and the statement kind.
    await _sweep(
        db_pool,
        [
            sf.finding(sf.UNMATCHED_ROWS, other, f"3 unmatched rows on {other}", payload={"rows": 3}),
            sf.finding(sf.UNPARSED_FILE, f"{other}/x.pdf", "x.pdf did not parse (unreadable)"),
        ],
        now=NOW + timedelta(minutes=5),
    )
    assert (await get_problem(db_pool, result.problem_id))["status"] == "open"

    # It ends when its own statement reconciles.
    cleared = await sf.clear_closing_balance(
        db_pool, statement_id=statement_id, now=NOW + timedelta(hours=1), project=False
    )
    assert cleared.action == "resolved"
    assert (await get_problem(db_pool, result.problem_id))["status"] == "resolved"


async def test_a_later_statement_does_not_clear_an_earlier_mismatch(db_pool):
    """August passing says nothing about July: July's period is still
    unreconciled, and closing it would erase the only record that it never
    was."""
    inst = _instrument()
    july = await sf.record_closing_balance(
        db_pool,
        statement_id=statements.statement_id_for(inst, *JULY),
        instrument=inst,
        reason="difference -10",
        now=NOW,
        project=False,
    )
    await sf.clear_closing_balance(
        db_pool,
        statement_id=statements.statement_id_for(inst, *AUGUST),
        instrument=inst,
        now=NOW + timedelta(hours=1),
        project=False,
    )
    assert (await get_problem(db_pool, july.problem_id))["status"] == "open"


async def test_a_sweep_refuses_to_carry_an_arrival_class(db_pool):
    with pytest.raises(ValueError, match="arrival-time class"):
        await _sweep(db_pool, [sf.finding(sf.CLOSING_BALANCE, "s", "t")])


async def test_each_subject_kind_gets_its_own_call(db_pool):
    """Trap 2. `reconcile_findings` takes one subject kind per call, so the
    file-scoped classes need their own. Fold both class lists into one
    instrument-scoped call and the file finding is stored as an `instrument`
    problem, and no call ever resolves it — the resolution query filters on the
    kind, and a `statement` problem never appears in an `instrument` one."""
    inst, path = _instrument(), f"{_instrument()}/july.pdf"
    account_f = sf.finding(sf.UNMATCHED_ROWS, inst, f"3 unmatched rows on {inst}", payload={"rows": 3})
    file_f = sf.finding(sf.UNPARSED_FILE, path, "july.pdf did not parse (unreadable)")

    first = await _sweep(db_pool, [account_f, file_f])
    account = next(f for f in first[sf.INSTRUMENT]["fresh"] if f["subject"] == inst)
    file_ = next(f for f in first[sf.STATEMENT]["fresh"] if f["subject"] == path)
    assert (await get_problem(db_pool, account["problem_id"]))["subject_kind"] == sf.INSTRUMENT
    assert (await get_problem(db_pool, file_["problem_id"]))["subject_kind"] == sf.STATEMENT

    # The file is fixed, the account still has rows. Only the statement-scoped
    # call can end the file's problem, and it does.
    await _sweep(db_pool, [account_f], now=NOW + timedelta(minutes=5))
    assert (await get_problem(db_pool, file_["problem_id"]))["status"] == "resolved"
    assert (await get_problem(db_pool, account["problem_id"]))["status"] == "open"

    # …and the other way round.
    await _sweep(db_pool, [file_f], now=NOW + timedelta(minutes=10))
    assert (await get_problem(db_pool, file_["problem_id"]))["status"] == "open"
    assert (await get_problem(db_pool, account["problem_id"]))["status"] == "resolved"


async def test_an_unparsed_file_survives_an_instrument_only_tick(db_pool):
    """The brief's half of trap 2: a tick that evaluated only the matcher must
    say so, and then the file-scoped problems are untouched — in both
    directions."""
    inst, path = _instrument(), f"{_instrument()}/august.pdf"
    first = await _sweep(
        db_pool,
        [
            sf.finding(sf.UNMATCHED_ROWS, inst, f"3 unmatched rows on {inst}", payload={"rows": 3}),
            sf.finding(sf.UNPARSED_FILE, path, "august.pdf did not parse (unreadable)"),
        ],
    )
    account = next(f for f in first[sf.INSTRUMENT]["fresh"] if f["subject"] == inst)
    file_ = next(f for f in first[sf.STATEMENT]["fresh"] if f["subject"] == path)

    await _sweep(db_pool, [], kinds=(sf.INSTRUMENT,), now=NOW + timedelta(minutes=5))
    assert (await get_problem(db_pool, file_["problem_id"]))["status"] == "open"
    assert (await get_problem(db_pool, account["problem_id"]))["status"] == "resolved"

    await _sweep(db_pool, [], kinds=(sf.STATEMENT,), now=NOW + timedelta(minutes=10))
    assert (await get_problem(db_pool, file_["problem_id"]))["status"] == "resolved"


async def test_a_kind_this_tick_did_not_evaluate_is_left_alone(db_pool):
    """A matcher-only run must say so, or the empty statement-scoped findings
    list would report every locked file as fixed."""
    path = f"{_instrument()}/locked.pdf"
    first = await _sweep(
        db_pool, [sf.finding(sf.PASSWORD_FAILED, path, "locked.pdf is locked")]
    )
    pid = first[sf.STATEMENT]["fresh"][0]["problem_id"]

    out = await _sweep(db_pool, [], kinds=(sf.INSTRUMENT,), now=NOW + timedelta(minutes=5))
    assert set(out) == {sf.INSTRUMENT}
    assert (await get_problem(db_pool, pid))["status"] == "open"


async def test_a_class_this_tick_did_not_evaluate_is_left_alone(db_pool):
    """#491, one level down. Coverage shares the instrument kind with the
    matcher's classes, so a tick that skipped coverage cannot say so by
    dropping the kind — that would stop `unmatched_rows` resolving too. It
    names the class instead, and only that class is left alone."""
    missing, unmatched = _instrument(), _instrument()
    first = await _sweep(
        db_pool,
        [
            sf.finding(sf.STATEMENT_MISSING, missing, f"No statement for {missing}"),
            sf.finding(sf.UNMATCHED_ROWS, unmatched, f"3 unmatched rows on {unmatched}"),
        ],
    )
    ids = {f["subject"]: f["problem_id"] for f in first[sf.INSTRUMENT]["fresh"]}

    await _sweep(
        db_pool,
        [],
        kinds=(sf.INSTRUMENT,),
        unevaluated={sf.STATEMENT_MISSING},
        now=NOW + timedelta(minutes=5),
    )
    assert (await get_problem(db_pool, ids[missing]))["status"] == "open"
    assert (await get_problem(db_pool, ids[unmatched]))["status"] == "resolved"

    # A tick that did look, and found nothing, ends it.
    await _sweep(db_pool, [], kinds=(sf.INSTRUMENT,), now=NOW + timedelta(minutes=10))
    assert (await get_problem(db_pool, ids[missing]))["status"] == "resolved"


async def test_the_unmatched_count_falls_and_the_problem_resolves_at_zero(db_pool):
    """The recovery half, driven by real match runs: three rows nothing
    matched, then two rules land, then all three. The count in the title has to
    follow, or the task lies about the size of the backlog for ever."""
    inst = _instrument()
    rows = [make_row(inst, 3, "100.00"), make_row(inst, 4, "200.00"), make_row(inst, 5, "300.00")]

    first = await _sweep(db_pool, sf.match_findings(run_match(rows)))
    pid = next(f for f in first[sf.INSTRUMENT]["fresh"] if f["subject"] == inst)["problem_id"]
    assert (await get_problem(db_pool, pid))["title"] == f"3 unmatched rows on {inst}"

    # Two of the three now have a journal counterpart.
    partly = run_match(
        rows,
        [make_candidate("m1", inst, 3, "100.00"), make_candidate("m2", inst, 4, "200.00")],
    )
    await _sweep(db_pool, sf.match_findings(partly), now=NOW + timedelta(minutes=5))
    problem = await get_problem(db_pool, pid)
    assert problem["status"] == "open"
    assert problem["title"] == f"1 unmatched row on {inst}"

    # The last one lands: no finding, so the problem recovers on its own.
    whole = run_match(
        rows,
        [
            make_candidate("m1", inst, 3, "100.00"),
            make_candidate("m2", inst, 4, "200.00"),
            make_candidate("m3", inst, 5, "300.00"),
        ],
    )
    assert [f for f in sf.match_findings(whole) if f["klass"] == sf.UNMATCHED_ROWS] == []
    last = await _sweep(db_pool, sf.match_findings(whole), now=NOW + timedelta(minutes=10))
    assert pid in [r["problem_id"] for r in last[sf.INSTRUMENT]["resolved"]]
    assert (await get_problem(db_pool, pid))["status"] == "resolved"


async def test_a_later_row_joins_the_account_problem_instead_of_opening_one(db_pool):
    """The 215th row joins the existing problem — that is what makes one
    problem per account bearable at 959 rows."""
    inst = _instrument()
    rows = [make_row(inst, 3, "100.00")]
    first = await _sweep(db_pool, sf.match_findings(run_match(rows)))
    pid = next(f for f in first[sf.INSTRUMENT]["fresh"] if f["subject"] == inst)["problem_id"]

    rows.append(make_row(inst, 6, "400.00"))
    again = await _sweep(db_pool, sf.match_findings(run_match(rows)), now=NOW + timedelta(minutes=5))
    assert [f["subject"] for f in again[sf.INSTRUMENT]["fresh"]] == []
    problem = await get_problem(db_pool, pid)
    assert problem["occurrences"] == 2
    assert problem["title"] == f"2 unmatched rows on {inst}"


# --- a ticked-off task acknowledges its finding ------------------------------


@pytest_asyncio.fixture(loop_scope="function")
async def ticks(db_pool):
    yield
    await db_pool.execute("DELETE FROM todoist_tasks WHERE id LIKE 'zzsf-task-%'")


async def _tick_off(pool, problem_id: str, at: datetime) -> None:
    """The real path: a person completes the problem's task, the sync mirrors
    it, and the hub sweep reads the completion back as a resolve."""
    task = f"zzsf-task-{uuid.uuid4().hex[:10]}"
    await pool.execute(
        "UPDATE problems SET todoist_task_id = $2 WHERE id = $1::uuid", problem_id, task
    )
    await pool.execute(
        "INSERT INTO todoist_tasks (id, content, labels, source_tag, is_completed, "
        "completed_at, raw) VALUES ($1, 'money', ARRAY['#money','@maou','@next'], '#money', "
        "true, $2, '{}'::jsonb)",
        task,
        at,
    )
    done = await hub_project.reconcile_completed_tasks(pool, now=at)
    assert problem_id in [d["problem_id"] for d in done if d["action"] == "resolved"]


async def _actions(pool, problem_id: str) -> list[str]:
    return [
        r["action"]
        for r in await pool.fetch(
            "SELECT payload->>'action' AS action FROM problem_events "
            "WHERE problem_id = $1::uuid AND kind = 'state_change' ORDER BY id",
            problem_id,
        )
    ]


def _fresh(out, subject: str, klass: str = sf.UNMATCHED_ROWS) -> str:
    """The problem a sweep opened for one finding. Filtered by class as well as
    subject: a match run on an account with no entity scope also finds
    `unscoped_instrument` on the same subject, and a fresh one of those would
    otherwise stand in for an unmatched-rows problem that never came back."""
    found = [
        f["problem_id"]
        for f in out[sf.INSTRUMENT]["fresh"]
        if f["subject"] == subject and f["klass"] == klass
    ]
    assert found, f"no fresh {klass} problem for {subject}"
    return found[0]


async def test_a_ticked_off_money_task_stays_quiet_until_a_new_row_turns_up(db_pool, ticks):
    """The bug itself (prod 2026-09-11). Completing a money task resolved its
    problem, and the next statement run found the same rows and reopened the
    problem and the task, so ticking one off was pointless."""
    inst = _instrument()
    rows = [make_row(inst, 3, "100.00"), make_row(inst, 4, "200.00")]
    pid = _fresh(await _sweep(db_pool, sf.match_findings(run_match(rows))), inst)
    await _tick_off(db_pool, pid, NOW + timedelta(minutes=5))

    # The next run finds the same two rows: nothing new, so nothing happens.
    await _sweep(db_pool, sf.match_findings(run_match(rows)), now=NOW + timedelta(hours=1))
    problem = await get_problem(db_pool, pid)
    assert problem["status"] == "resolved"
    assert problem["occurrences"] == 1
    assert "reopen" not in await _actions(db_pool, pid)

    # A third row turns up. That is new, so the problem comes back.
    rows.append(make_row(inst, 5, "300.00"))
    await _sweep(db_pool, sf.match_findings(run_match(rows)), now=NOW + timedelta(hours=2))
    problem = await get_problem(db_pool, pid)
    assert problem["status"] == "open"
    assert problem["title"] == f"3 unmatched rows on {inst}"
    assert "reopen" in await _actions(db_pool, pid)


async def test_an_acknowledgement_outlives_the_seven_day_close(db_pool, ticks):
    """A closed problem keeps its row, so the acknowledgement still holds once
    the close has freed the key. Only something new opens a fresh problem."""
    inst = _instrument()
    rows = [make_row(inst, 3, "100.00")]
    pid = _fresh(await _sweep(db_pool, sf.match_findings(run_match(rows))), inst)
    await _tick_off(db_pool, pid, NOW + timedelta(minutes=5))
    assert await close_problem(db_pool, pid, now=NOW + timedelta(days=8))

    later = NOW + timedelta(days=9)
    out = await _sweep(db_pool, sf.match_findings(run_match(rows)), now=later)
    assert not [f for f in out[sf.INSTRUMENT]["fresh"] if f["subject"] == inst]
    key = sf.match_findings(run_match(rows))[0]
    count = "SELECT count(*) FROM problems WHERE correlation_key = $1"
    assert await db_pool.fetchval(count, sf._key_for(key)) == 1

    rows.append(make_row(inst, 6, "400.00"))
    out = await _sweep(db_pool, sf.match_findings(run_match(rows)), now=later + timedelta(hours=1))
    assert _fresh(out, inst) != pid
    assert await db_pool.fetchval(count, sf._key_for(key)) == 2


@pytest.mark.parametrize("newer_closed", [False, True])
async def test_a_newer_problem_the_watchdog_resolved_is_not_acknowledged(
    db_pool, ticks, newer_closed
):
    """Only the NEWEST problem for a key speaks for it. An older problem a
    person ticked off, and the seven-day close retired, must not cover a newer
    one the watchdog resolved when its rows matched: a row that comes back
    after that is news. The newer problem wins whether it is still live or
    closed too.

    Falsifiable: put closed problems first and the live case drops the row;
    order by `first_seen_at` ascending and the all-closed case does.
    """
    inst = _instrument()
    a, b = make_row(inst, 3, "100.00"), make_row(inst, 4, "200.00")
    p1 = _fresh(await _sweep(db_pool, sf.match_findings(run_match([a]))), inst)
    await _tick_off(db_pool, p1, NOW + timedelta(minutes=5))
    assert await close_problem(db_pool, p1, now=NOW + timedelta(days=8))

    t = NOW + timedelta(days=9)
    p2 = _fresh(await _sweep(db_pool, sf.match_findings(run_match([a, b])), now=t), inst)
    assert p2 != p1
    # Every row matched: the watchdog resolves P2, not a person.
    await _sweep(db_pool, [], kinds=(sf.INSTRUMENT,), now=t + timedelta(hours=1))
    assert (await get_problem(db_pool, p2))["status"] == "resolved"
    if newer_closed:
        assert await close_problem(db_pool, p2, now=t + timedelta(days=8))

    # Row A comes back. P1's acknowledgement must not cover it.
    back = t + timedelta(days=9 if newer_closed else 0, hours=2)
    out = await _sweep(db_pool, sf.match_findings(run_match([a])), now=back)
    if newer_closed:
        assert _fresh(out, inst) not in (p1, p2)
    else:
        assert (await get_problem(db_pool, p2))["status"] == "open"


async def test_a_watchdog_resolve_after_a_persons_resolve_ends_the_acknowledgement(
    db_pool, ticks
):
    """Only the LATEST resolve speaks for a problem. A person ticked it off, a
    new row brought it back, then every row matched and the watchdog resolved
    it. When the first rows return, the person's tick no longer covers them.

    Falsifiable: read the first resolve instead of the last and they are
    dropped.
    """
    inst = _instrument()
    a, b, c = make_row(inst, 3, "100.00"), make_row(inst, 4, "200.00"), make_row(inst, 5, "300.00")
    pid = _fresh(await _sweep(db_pool, sf.match_findings(run_match([a, b]))), inst)
    await _tick_off(db_pool, pid, NOW + timedelta(minutes=5))
    await _sweep(db_pool, sf.match_findings(run_match([a, b, c])), now=NOW + timedelta(hours=1))
    assert (await get_problem(db_pool, pid))["status"] == "open"
    await _sweep(db_pool, [], kinds=(sf.INSTRUMENT,), now=NOW + timedelta(hours=2))
    assert (await get_problem(db_pool, pid))["status"] == "resolved"

    await _sweep(db_pool, sf.match_findings(run_match([a, b])), now=NOW + timedelta(hours=3))
    assert (await get_problem(db_pool, pid))["status"] == "open"


@pytest.mark.parametrize("shape", ["empty", "shorter-than-its-count"])
async def test_a_row_list_that_disagrees_with_its_count_is_never_acknowledged(
    db_pool, ticks, shape
):
    """An empty list is a subset of anything, and a short one hides the rows it
    left out. `match_findings` builds the ids and the count from the same
    outcomes, so either shape means something upstream went wrong, and the
    finding is reported rather than guessed quiet.

    Falsifiable: drop the check in `_items` and both are taken as acknowledged.
    """
    inst = _instrument()
    a, b = make_row(inst, 3, "100.00"), make_row(inst, 4, "200.00")
    pid = _fresh(await _sweep(db_pool, sf.match_findings(run_match([a, b]))), inst)
    await _tick_off(db_pool, pid, NOW + timedelta(minutes=5))
    # Both are subsets of what the person ticked off, so only the check stops them.
    payload = {"rows": 0, "row_ids": []} if shape == "empty" else {"rows": 2, "row_ids": [a.row_id]}
    odd = sf.finding(sf.UNMATCHED_ROWS, inst, f"unmatched rows on {inst}", payload=payload)
    await _sweep(db_pool, [odd], now=NOW + timedelta(hours=1))
    assert (await get_problem(db_pool, pid))["status"] == "open"


async def test_a_ticked_off_missing_statement_comes_back_for_the_next_month(db_pool, ticks):
    """A missing statement is acknowledged for its period, not for good."""
    inst = _instrument()
    august = sf.missing_statement_findings([inst], [], period="2026-08")
    pid = _fresh(await _sweep(db_pool, august), inst, sf.STATEMENT_MISSING)
    await _tick_off(db_pool, pid, NOW + timedelta(minutes=5))

    await _sweep(db_pool, august, now=NOW + timedelta(hours=1))
    assert (await get_problem(db_pool, pid))["status"] == "resolved"

    september = sf.missing_statement_findings([inst], [], period="2026-09")
    await _sweep(db_pool, september, now=NOW + timedelta(hours=2))
    assert (await get_problem(db_pool, pid))["status"] == "open"


async def test_a_ticked_off_subject_finding_stays_quiet(db_pool, ticks):
    """Every other class is about its subject alone, and the subject is in the
    key, so the same finding again is nothing new."""
    inst = _instrument()
    unscoped = [
        sf.finding(sf.UNSCOPED_INSTRUMENT, inst, f"No entity declared for {inst}",
                   payload={"instrument": inst})
    ]
    pid = _fresh(await _sweep(db_pool, unscoped), inst, sf.UNSCOPED_INSTRUMENT)
    await _tick_off(db_pool, pid, NOW + timedelta(minutes=5))
    await _sweep(db_pool, unscoped, now=NOW + timedelta(hours=1))
    assert (await get_problem(db_pool, pid))["status"] == "resolved"
    assert "reopen" not in await _actions(db_pool, pid)


async def test_only_a_persons_completion_acknowledges(db_pool):
    """A problem the sweep resolved because its rows matched, and whose rows
    then come back, reopens exactly as it always did."""
    inst = _instrument()
    rows = [make_row(inst, 3, "100.00")]
    pid = _fresh(await _sweep(db_pool, sf.match_findings(run_match(rows))), inst)
    await _sweep(db_pool, [], kinds=(sf.INSTRUMENT,), now=NOW + timedelta(minutes=5))
    assert (await get_problem(db_pool, pid))["status"] == "resolved"

    await _sweep(db_pool, sf.match_findings(run_match(rows)), now=NOW + timedelta(hours=1))
    assert (await get_problem(db_pool, pid))["status"] == "open"
    assert "reopen" in await _actions(db_pool, pid)


async def test_a_finding_without_its_row_ids_is_never_taken_as_acknowledged(db_pool, ticks):
    """Over the cap, or recorded before `row_ids` existed: with no list to
    compare, the finding is reported as before rather than guessed quiet."""
    inst = _instrument()
    capped = [
        sf.finding(sf.UNMATCHED_ROWS, inst, f"2001 unmatched rows on {inst}",
                   payload={"rows": 2001})
    ]
    pid = _fresh(await _sweep(db_pool, capped), inst)
    await _tick_off(db_pool, pid, NOW + timedelta(minutes=5))
    await _sweep(db_pool, capped, now=NOW + timedelta(hours=1))
    assert (await get_problem(db_pool, pid))["status"] == "open"


async def test_the_lane_projects_through_the_hub_projector(db_pool, monkeypatch):
    """§15.8: the money lane posts no comments of its own. Everything it puts
    in front of a human goes through `hub_project.project`, whose comments
    carry the `Workflow run: problem-hub` footer that clarify's loop guard and
    `work_sessions.is_user_note` exclude."""
    seen: list[str] = []

    async def fake_project(pool, problem_id, **kw):
        seen.append(problem_id)
        return {"problem_id": problem_id}

    monkeypatch.setattr(hub_project, "project", fake_project)
    inst = _instrument()
    out = await sf.sweep(
        db_pool,
        [sf.finding(sf.UNMATCHED_ROWS, inst, f"1 unmatched row on {inst}", payload={"rows": 1})],
        now=NOW,
        project=True,
    )
    pid = next(f for f in out[sf.INSTRUMENT]["fresh"] if f["subject"] == inst)["problem_id"]
    assert pid in seen

    seen.clear()
    mismatch = await sf.record_closing_balance(
        db_pool,
        statement_id=statements.statement_id_for(inst, *JULY),
        instrument=inst,
        reason="difference -10",
        now=NOW,
    )
    assert seen == [mismatch.problem_id]
    assert "Workflow run:" in hub_project.FOOTER


# --- the digest -------------------------------------------------------------


def test_the_monthly_digest_reports_the_run_without_alerting():
    inst = _instrument()
    row = make_row(inst, 3, "100.00")
    run = run_match(
        [row, make_row(inst, 4, "200.00")],
        [make_candidate("m1", inst, 3, "100.00"), make_candidate("m2", inst, 4, "100.00")],
    )
    report = IntakeReport(
        outcomes=[
            FileOutcome(
                file_id="f2", title="locked.pdf", folder="axis-9640", status=UNREADABLE,
                reason=f"{StatementLocked.__name__}: the statement could not be opened",
            )
        ]
    )
    text = sf.monthly_digest(run, report, period="2026-07")
    assert "2026-07" in text
    # One row is ambiguous and one unmatched, so nothing matched.
    assert "zzacct: 0/2 rows matched (0.0%)" in text
    assert f"{statements.statement_id_for(inst, *JULY)}: 1 of 2" in text
    assert "Ambiguous rows (1)" in text
    assert "m1, m2" in text
    assert "axis-9640/locked.pdf" in text
    # Pure: same run in, same report out, and no database anywhere near it.
    assert sf.monthly_digest(run, report, period="2026-07") == text


def test_the_digest_counts_the_statements_its_run_was_narrowed_from():
    """The activity hands the digest only the statements the lane can still act
    on, so in the goal state — everything in scope reconciled — the run is
    empty, and the digest used to say it saw no statements at all. The counts
    say what was narrowed away, and the empty state says it is good news."""
    empty = MatchRun(outcomes=(), summaries=(), claimed={})

    done = sf.monthly_digest(
        empty, period="2026-10", statements={"reconciled": 11, "out_of_scope": 2, "open": 0}
    )
    assert "13 statements: 11 reconciled, 2 out of scope, 0 open" in done
    assert "all in-scope statements reconcile with the books" in done
    assert "no statements in this run" not in done

    # An open statement whose every row the tick posted leaves the run empty
    # too, and it has not reconciled — the digest must not say it has.
    posted = sf.monthly_digest(
        empty, period="2026-10", statements={"reconciled": 0, "out_of_scope": 0, "open": 1}
    )
    assert "1 statement: 0 reconciled, 0 out of scope, 1 open" in posted
    assert "reconcile with the books" not in posted
