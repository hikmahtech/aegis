"""Following a fix PR from merge to verified (#502).

An investigation's Gate-2 "Open PR(s)" opens a pull request and records it on
the problem (`record_investigation` with `payload.pr_urls`, which also writes
the `github_pr` link). These pin what happens after: the GitHub webhook says
the PR closed, `hub_fix.record_pr_closed` moves the problem, and the hub
sweep's `hub_fix.verify_fixes` resolves it once the alert has stayed clear or
reopens it when it comes back. Real database, fixed clock.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from aegis.services import hub_fix
from aegis.services.hub import (
    Event,
    add_link,
    get_problem,
    ingest_event,
    list_events,
    set_service_state,
    set_status,
)

T0 = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)


def _subject() -> str:
    return f"svc_{uuid.uuid4().hex[:8]}"


def _pr_url() -> str:
    return f"https://github.com/acme/shop/pull/{uuid.uuid4().int % 100000}"


async def _problem(pool, subject: str, *, at: datetime = T0 - timedelta(hours=3)) -> str:
    r = await ingest_event(
        pool,
        Event(
            source="sentry",
            external_id=f"{subject}@{at.isoformat()}",
            kind="occurrence",
            title=f"TimeoutError in {subject}",
            klass="TimeoutError",
            subject=subject,
            occurred_at=at,
        ),
        now=at,
    )
    return r.problem_id


async def _occur(pool, subject: str, at: datetime) -> None:
    await ingest_event(
        pool,
        Event(
            source="sentry",
            external_id=f"{subject}@{at.isoformat()}",
            kind="occurrence",
            title=f"TimeoutError in {subject}",
            klass="TimeoutError",
            subject=subject,
            occurred_at=at,
        ),
        now=at,
    )


async def _open_pr(pool, problem_id: str, url: str, *, at: datetime = T0 - timedelta(hours=2)) -> None:
    """What the flow's `prs_opened` step writes through `record_investigation`:
    an investigation event carrying `pr_urls`, the `github_pr` link, and the
    move to `fixing`."""
    await ingest_event(
        pool,
        Event(
            source="investigation",
            external_id=f"wf-{uuid.uuid4().hex[:6]}:prs_opened",
            kind="investigation",
            title="1 PR(s) opened",
            payload={"pr_urls": [url], "text": f"1 PR(s) opened: {url}", "status": "fixing", "posted": True},
            occurred_at=at,
            problem_id=problem_id,
        ),
        now=at,
    )
    await add_link(pool, problem_id, "github_pr", url)
    await set_status(pool, problem_id, "fixing", reason="PR opened", now=at)


async def _notes(pool, problem_id: str) -> list[dict]:
    return [
        e
        for e in await list_events(pool, problem_id, limit=200)
        if e["kind"] == "investigation" and e["source"] in {"github", "hub"}
    ]


# --- pure ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ({"a": "merged"}, "verifying"),
        ({"a": "closed"}, "waiting_human"),
        ({"a": "open"}, "fixing"),
        ({"a": "merged", "b": "open"}, "fixing"),
        ({"a": "closed", "b": "open"}, "fixing"),
        ({"a": "merged", "b": "closed"}, "verifying"),
        ({"a": "closed", "b": "closed"}, "waiting_human"),
    ],
)
def test_fix_status(states, expected):
    """A PR still open means the fix is still being made; once none is open,
    one merge is enough to watch the alert, and no merge at all hands it back."""
    assert hub_fix.fix_status(states) == expected


def test_pr_urls_compare_without_case_or_a_trailing_slash():
    assert hub_fix.normalize_pr_url(" https://GitHub.com/Acme/Shop/pull/5/ ") == (
        "https://github.com/acme/shop/pull/5"
    )
    assert hub_fix.normalize_pr_url("") == ""


# --- the webhook side -----------------------------------------------------------


async def test_a_merged_fix_pr_moves_its_problem_to_verifying(db_pool):
    pid = await _problem(db_pool, _subject())
    url = _pr_url()
    await _open_pr(db_pool, pid, url)

    out = await hub_fix.record_pr_closed(
        db_pool, url=url, merged=True, at=T0.isoformat(), now=T0
    )

    assert out == [{"problem_id": pid, "state": "merged", "status": "verifying", "moved": True}]
    assert (await get_problem(db_pool, pid))["status"] == "verifying"
    [note] = await _notes(db_pool, pid)
    assert note["source"] == "github"
    assert note["payload"]["pr"] == {"url": url, "state": "merged", "at": T0.isoformat()}
    # The projector posts it: the flow did not put it on the task itself.
    assert note["payload"]["posted"] is False
    assert "merged" in note["payload"]["text"] and url in note["payload"]["text"]


async def test_a_pr_closed_without_merging_hands_the_problem_back(db_pool):
    pid = await _problem(db_pool, _subject())
    url = _pr_url()
    await _open_pr(db_pool, pid, url)

    out = await hub_fix.record_pr_closed(db_pool, url=url, merged=False, at=T0.isoformat(), now=T0)

    assert [(r["state"], r["status"], r["moved"]) for r in out] == [("closed", "waiting_human", True)]
    assert (await get_problem(db_pool, pid))["status"] == "waiting_human"
    [note] = await _notes(db_pool, pid)
    assert "without merging" in note["payload"]["text"]


async def test_the_webhook_url_matches_whatever_case_gh_printed(db_pool):
    pid = await _problem(db_pool, _subject())
    url = _pr_url()
    await _open_pr(db_pool, pid, url.replace("acme/shop", "Acme/Shop"))

    out = await hub_fix.record_pr_closed(db_pool, url=url + "/", merged=True, at="t", now=T0)

    assert [r["problem_id"] for r in out] == [pid]


async def test_a_pr_no_investigation_opened_changes_nothing(db_pool):
    """A coding session links its PR through `report_progress` (a bare
    `github_pr` link). Its merge says nothing about whether an alert is fixed,
    so the problem is left alone."""
    pid = await _problem(db_pool, _subject())
    url = _pr_url()
    await add_link(db_pool, pid, "github_pr", url)
    await set_status(db_pool, pid, "waiting_human", reason="card", now=T0 - timedelta(hours=1))

    assert await hub_fix.record_pr_closed(db_pool, url=url, merged=True, at="t", now=T0) == []
    assert await hub_fix.record_pr_closed(db_pool, url=_pr_url(), merged=True, at="t", now=T0) == []
    assert (await get_problem(db_pool, pid))["status"] == "waiting_human"
    assert await _notes(db_pool, pid) == []


async def test_a_redelivered_close_is_recorded_once(db_pool):
    pid = await _problem(db_pool, _subject())
    url = _pr_url()
    await _open_pr(db_pool, pid, url)

    await hub_fix.record_pr_closed(db_pool, url=url, merged=True, at=T0.isoformat(), now=T0)
    again = await hub_fix.record_pr_closed(
        db_pool, url=url, merged=True, at=T0.isoformat(), now=T0 + timedelta(seconds=5)
    )

    assert [r["moved"] for r in again] == [False]
    assert len(await _notes(db_pool, pid)) == 1


async def test_a_problem_the_alert_already_resolved_stays_resolved(db_pool):
    """#488: the alert source owns whether a problem is live. A merge after
    the alert cleared is history on the timeline, not a reopen."""
    subject = _subject()
    pid = await _problem(db_pool, subject)
    url = _pr_url()
    await _open_pr(db_pool, pid, url)
    await set_status(
        db_pool, pid, "resolved", reason="the alert cleared", source="sentry", now=T0 - timedelta(hours=1)
    )

    out = await hub_fix.record_pr_closed(db_pool, url=url, merged=True, at="t", now=T0)

    assert [(r["status"], r["moved"]) for r in out] == [("resolved", False)]
    p = await get_problem(db_pool, pid)
    assert p["status"] == "resolved" and p["resolved_at"] is not None
    [note] = await _notes(db_pool, pid)
    assert "already cleared" in note["payload"]["text"]


async def test_one_of_two_fix_prs_merging_waits_for_the_other(db_pool):
    pid = await _problem(db_pool, _subject())
    first, second = _pr_url(), _pr_url()
    await _open_pr(db_pool, pid, first)
    await _open_pr(db_pool, pid, second, at=T0 - timedelta(hours=1))

    out = await hub_fix.record_pr_closed(db_pool, url=first, merged=True, at="a", now=T0)
    assert [(r["status"], r["moved"]) for r in out] == [("fixing", False)]
    assert (await get_problem(db_pool, pid))["status"] == "fixing"
    assert "1 more" in (await _notes(db_pool, pid))[0]["payload"]["text"]

    # The other one is turned down: one fix merged, so the alert is watched.
    out = await hub_fix.record_pr_closed(
        db_pool, url=second, merged=False, at="b", now=T0 + timedelta(minutes=5)
    )
    assert [(r["status"], r["moved"]) for r in out] == [("verifying", True)]


# --- the sweep side -------------------------------------------------------------


async def _verifying(pool) -> tuple[str, str, str]:
    """A problem whose fix PR merged at T0."""
    subject = _subject()
    pid = await _problem(pool, subject)
    url = _pr_url()
    await _open_pr(pool, pid, url)
    await hub_fix.record_pr_closed(pool, url=url, merged=True, at=T0.isoformat(), now=T0)
    assert (await get_problem(pool, pid))["status"] == "verifying"
    return pid, subject, url


def _row(rows: list[dict], pid: str) -> dict | None:
    return next((r for r in rows if r["problem_id"] == pid), None)


async def test_a_fix_that_stayed_clear_for_the_window_resolves(db_pool):
    pid, _, url = await _verifying(db_pool)

    early = await hub_fix.verify_fixes(
        db_pool, window_hours=24, grace_hours=1, now=T0 + timedelta(hours=23)
    )
    assert _row(early, pid) is None, "too soon to tell"
    assert (await get_problem(db_pool, pid))["status"] == "verifying"

    rows = await hub_fix.verify_fixes(
        db_pool, window_hours=24, grace_hours=1, now=T0 + timedelta(hours=24, minutes=1)
    )

    assert _row(rows, pid) == {"problem_id": pid, "action": "resolved"}
    assert (await get_problem(db_pool, pid))["status"] == "resolved"
    note = (await _notes(db_pool, pid))[0]
    assert note["source"] == "hub" and note["payload"]["posted"] is False
    assert "stayed clear for 24h" in note["payload"]["text"] and url in note["payload"]["text"]
    # And the sweep asking again finds nothing to do.
    again = await hub_fix.verify_fixes(
        db_pool, window_hours=24, grace_hours=1, now=T0 + timedelta(hours=25)
    )
    assert _row(again, pid) is None
    assert len(await _notes(db_pool, pid)) == 2  # the merge and the verdict


async def test_it_came_back_after_the_fix_so_it_reopens(db_pool):
    pid, subject, url = await _verifying(db_pool)
    await _occur(db_pool, subject, T0 + timedelta(hours=3))
    # A recurrence ATTACHES to a live problem, so on its own it changes nothing.
    assert (await get_problem(db_pool, pid))["status"] == "verifying"

    rows = await hub_fix.verify_fixes(
        db_pool, window_hours=24, grace_hours=1, now=T0 + timedelta(hours=3, minutes=5)
    )

    assert _row(rows, pid) == {"problem_id": pid, "action": "reopened"}
    assert (await get_problem(db_pool, pid))["status"] == "open"
    text = (await _notes(db_pool, pid))[0]["payload"]["text"]
    assert "came back" in text and url in text and "3.0h after the fix merged" in text


async def test_an_occurrence_before_the_fix_could_ship_does_not_count(db_pool):
    """Inside the grace after the merge the old code is usually still
    running: CI has not built it, or the deploy has not rolled out."""
    pid, subject, _ = await _verifying(db_pool)
    await _occur(db_pool, subject, T0 + timedelta(minutes=30))

    rows = await hub_fix.verify_fixes(
        db_pool, window_hours=24, grace_hours=1, now=T0 + timedelta(hours=25)
    )

    assert _row(rows, pid) == {"problem_id": pid, "action": "resolved"}


async def test_an_occurrence_during_a_deploy_window_does_not_count(db_pool):
    pid, subject, _ = await _verifying(db_pool)
    await set_service_state(
        db_pool, subject, "deploying", minutes=30, set_by="ansible", now=T0 + timedelta(hours=2)
    )
    await _occur(db_pool, subject, T0 + timedelta(hours=2, minutes=5))
    await set_service_state(db_pool, subject, "ok", set_by="ansible", now=T0 + timedelta(hours=2, minutes=20))

    rows = await hub_fix.verify_fixes(
        db_pool, window_hours=24, grace_hours=1, now=T0 + timedelta(hours=25)
    )

    assert _row(rows, pid) == {"problem_id": pid, "action": "resolved"}


async def test_a_zero_grace_counts_any_occurrence_after_the_merge(db_pool):
    pid, subject, _ = await _verifying(db_pool)
    await _occur(db_pool, subject, T0 + timedelta(minutes=10))

    rows = await hub_fix.verify_fixes(
        db_pool, window_hours=24, grace_hours=0, now=T0 + timedelta(minutes=15)
    )

    assert _row(rows, pid) == {"problem_id": pid, "action": "reopened"}
