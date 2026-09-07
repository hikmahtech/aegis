"""The defects the end-of-programme validation found, each pinned by the case
that used to be wrong.

Every test here fails on the code as it stood before PR 8, which is the only
reason to keep them: they are not variations on the happy paths the other hub
test files already cover.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from aegis.services import hub_project
from aegis.services.hub import (
    Event,
    close_problem,
    get_problem,
    ingest_event,
    list_events,
    set_service_state,
    set_status,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)


def _subject() -> str:
    return f"svc_{uuid.uuid4().hex[:8]}"


def _occ(subject: str, n: int = 1, **kw) -> Event:
    return Event(
        source="heartbeat",
        external_id=f"{subject}@{n}",
        kind="occurrence",
        title=f"Service {subject} down",
        klass="DockerServiceDown",
        subject=subject,
        severity="critical",
        occurred_at=kw.pop("occurred_at", NOW),
        **kw,
    )


async def _task(pool, task_id: str, content: str = "fix it") -> None:
    await pool.execute(
        "INSERT INTO todoist_tasks (id, content, labels, is_completed, updated_at) "
        "VALUES ($1, $2, ARRAY['@pandora','@code'], false, now()) ON CONFLICT (id) DO NOTHING",
        task_id,
        content,
    )


# --- 1. a manual problem belongs to one task, not to a repo -------------------


async def test_two_tasks_in_one_repo_get_two_problems(db_pool):
    """`ensure_problem_for_task` keyed the problem on the REPO, so the second
    `@code` task in a repo attached to the first task's problem: its session
    notes, PR links and comments all landed on the other task, while the tool
    replied "recorded on task B".

    Falsifiable: key the subject on the repo again and both tasks share one
    problem id.
    """
    a, b = f"zza-{uuid.uuid4().hex[:6]}", f"zzb-{uuid.uuid4().hex[:6]}"
    await _task(db_pool, a, "first task")
    await _task(db_pool, b, "second task")

    pa = await hub_project.ensure_problem_for_task(db_pool, a, subject="hikmahtech/aegis")
    pb = await hub_project.ensure_problem_for_task(db_pool, b, subject="hikmahtech/aegis")

    assert pa is not None and pb is not None
    assert pa["id"] != pb["id"], "one problem per task"
    assert pa["todoist_task_id"] == a and pb["todoist_task_id"] == b
    assert pa["subject"] == f"task-{a}" and pb["subject"] == f"task-{b}"
    # The repo is still on the record, as context rather than identity.
    occ = [e for e in await list_events(db_pool, pa["id"]) if e["kind"] == "occurrence"][0]
    assert occ["payload"]["github_repo"] == "hikmahtech/aegis"


async def test_a_task_whose_problem_closed_gets_a_fresh_one(db_pool):
    """A closed problem is never projected again, so a session note attached to
    one lands nowhere. The task gets a new problem instead."""
    task = f"zzc-{uuid.uuid4().hex[:6]}"
    await _task(db_pool, task)
    first = await hub_project.ensure_problem_for_task(db_pool, task)
    await db_pool.execute(
        "UPDATE problems SET status = 'closed', closed_at = now() WHERE id = $1::uuid",
        first["id"],
    )
    second = await hub_project.ensure_problem_for_task(db_pool, task)
    assert second is not None and second["id"] != first["id"]
    assert second["closed_at"] is None


# --- 3. leaving `resolved` is a reopen ---------------------------------------


async def test_a_status_move_off_resolved_clears_it_and_reopens_the_task(db_pool):
    """An investigation reporting `fixing` after the alert already cleared left
    a stale `resolved_at` behind: `close_resolved` then never retired the
    problem, and the projector left its task completed while the problem was
    live, so the next occurrence attached to a closed task in silence."""
    r = await ingest_event(db_pool, _occ(_subject()), now=NOW)
    await set_status(db_pool, r.problem_id, "resolved", reason="recovered", now=NOW)
    assert (await get_problem(db_pool, r.problem_id))["resolved_at"] is not None

    assert await set_status(db_pool, r.problem_id, "fixing", reason="not really", now=NOW)

    p = await get_problem(db_pool, r.problem_id)
    assert p["status"] == "fixing"
    assert p["resolved_at"] is None, "a live problem is not a resolved one"
    latest = [e for e in await list_events(db_pool, r.problem_id) if e["kind"] == "state_change"][0]
    assert latest["payload"]["action"] == "reopen", "the projector uncompletes on this"

    # A move between two live statuses is not a reopen.
    await set_status(db_pool, r.problem_id, "waiting_human", reason="asked", now=NOW)
    latest = [e for e in await list_events(db_pool, r.problem_id) if e["kind"] == "state_change"][0]
    assert latest["payload"]["action"] == "set_status"


# --- 4. closing closes one problem -------------------------------------------


async def test_close_problem_closes_only_the_one_asked_for(db_pool):
    """The admin close button called `close_resolved(days=0)`, which closes
    EVERY resolved problem in the database — including ones whose resolution
    had not been projected, and a closed problem is never projected again."""
    mine = await ingest_event(db_pool, _occ(_subject()), now=NOW)
    bystander = await ingest_event(db_pool, _occ(_subject()), now=NOW)
    for r in (mine, bystander):
        await set_status(db_pool, r.problem_id, "resolved", reason="fixed", now=NOW)

    assert await close_problem(db_pool, mine.problem_id, now=NOW) is True

    assert (await get_problem(db_pool, mine.problem_id))["status"] == "closed"
    assert (await get_problem(db_pool, bystander.problem_id))["status"] == "resolved"
    assert await close_problem(db_pool, mine.problem_id, now=NOW) is False, "already closed"
    assert await close_problem(db_pool, bystander.problem_id, now=NOW) is True

    live = await ingest_event(db_pool, _occ(_subject()), now=NOW)
    assert await close_problem(db_pool, live.problem_id, now=NOW) is False, "not resolved"
    assert await close_problem(db_pool, str(uuid.uuid4()), now=NOW) is False


# --- 5. concurrent delivery of one event counts once --------------------------


async def test_the_same_event_delivered_twice_at_once_counts_once(db_pool):
    """The duplicate claim used to be read BEFORE the advisory lock, so two
    simultaneous deliveries of one event both saw "not a duplicate", then
    serialised and both counted an occurrence while the second event insert
    silently did nothing.

    Falsifiable: move the claim check back above the lock and `occurrences`
    reaches 2.
    """
    s = _subject()
    first = await ingest_event(db_pool, _occ(s), now=NOW)
    event = _occ(s, 2)
    results = await asyncio.gather(
        ingest_event(db_pool, event, now=NOW),
        ingest_event(db_pool, event, now=NOW),
    )
    assert {r.problem_id for r in results} == {first.problem_id}
    assert sorted(r.action for r in results) == ["attached", "duplicate"]
    assert (await get_problem(db_pool, first.problem_id))["occurrences"] == 2


# --- 7a. one subject kind, at ingest and at promotion -------------------------


async def test_a_subject_less_event_is_not_promoted_out_of_a_live_window(db_pool):
    """The suppression lookup asked about kind `service` while the row was
    stored with kind `""`, so a wildcard window suppressed the occurrence and
    the next sweep promoted it back while the window was still in force."""
    from aegis.services.hub import promote_expired_suppressions

    await set_service_state(
        db_pool, "*", "maintenance", subject_kind="*", minutes=60, set_by="test", now=NOW
    )
    try:
        r = await ingest_event(
            db_pool,
            Event(
                source="sentry",
                external_id=f"nosubject-{uuid.uuid4().hex[:8]}",
                kind="occurrence",
                title="ValueError in a service nobody named",
                klass="ValueError",
                occurred_at=NOW,
            ),
            now=NOW,
        )
        assert r.suppressed is True
        assert (await get_problem(db_pool, r.problem_id))["status"] == "suppressed"

        promoted = await promote_expired_suppressions(db_pool, now=NOW + timedelta(minutes=1))
        assert r.problem_id not in promoted, "the window is still in force"
        assert (await get_problem(db_pool, r.problem_id))["status"] == "suppressed"
    finally:
        await set_service_state(db_pool, "*", "ok", subject_kind="*", set_by="test", now=NOW)
