"""`reconcile_findings`: a watchdog's current findings in, fresh problems and
recoveries out. Real test database; projection is switched off (the projector
has its own tests) except where the test is about it."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from aegis.services.hub import get_problem, mute_problem, set_service_state
from aegis.services.hub_watch import mute_hint, reconcile_findings

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
SRC = "flow_health"
CLASSES = ["flow_failing", "flow_stale"]


def _f(subject: str, klass: str = "flow_failing", **payload) -> dict:
    return {"klass": klass, "subject": subject, "title": f"{klass}: {subject}", "payload": payload}


def _subject() -> str:
    return f"zzwd_{uuid.uuid4().hex[:8]}"


async def _rec(pool, findings, now=NOW, **kw):
    return await reconcile_findings(
        pool, source=SRC, subject_kind="flow", classes=CLASSES, findings=findings, now=now,
        project=False, **kw,
    )


async def test_first_sighting_is_fresh_then_attached(db_pool):
    s = _subject()
    first = await _rec(db_pool, [_f(s, consecutive=2)])
    assert [f["subject"] for f in first["fresh"]] == [s]
    pid = first["fresh"][0]["problem_id"]
    p = await get_problem(db_pool, pid)
    assert (p["class"], p["subject"], p["subject_kind"]) == ("flow_failing", s, "flow")
    assert p["title"] == f"flow_failing: {s}"
    for n in range(1, 4):
        again = await _rec(db_pool, [_f(s)], now=NOW + timedelta(minutes=n))
        assert again["fresh"] == [] and again["attached"] == 1 and again["resolved"] == []
    assert (await get_problem(db_pool, pid))["occurrences"] == 4


async def test_disappearing_finding_resolves_and_recurrence_is_fresh_again(db_pool):
    s = _subject()
    first = await _rec(db_pool, [_f(s)])
    pid = first["fresh"][0]["problem_id"]
    gone = await _rec(db_pool, [], now=NOW + timedelta(hours=1))
    assert [r["subject"] for r in gone["resolved"]] == [s]
    assert gone["resolved"][0]["problem_id"] == pid
    assert (await get_problem(db_pool, pid))["status"] == "resolved"
    # nothing to resolve twice
    assert (await _rec(db_pool, [], now=NOW + timedelta(hours=2)))["resolved"] == []
    # back inside the reopen window: the same problem, a fresh card
    back = await _rec(db_pool, [_f(s)], now=NOW + timedelta(hours=3))
    assert back["fresh"][0]["problem_id"] == pid
    assert (await get_problem(db_pool, pid))["status"] == "open"


async def test_only_this_watchdogs_classes_are_resolved(db_pool):
    s = _subject()
    await _rec(db_pool, [_f(s, "flow_stale")])
    # a problem of another class on the same kind is not this watchdog's to resolve
    other = await reconcile_findings(
        db_pool, source="social", subject_kind="flow", classes=["stuck_post"],
        findings=[_f(s, "stuck_post")], now=NOW, project=False,
    )
    assert other["fresh"]
    gone = await _rec(db_pool, [], now=NOW + timedelta(minutes=5))
    assert [(r["klass"], r["subject"]) for r in gone["resolved"]] == [("flow_stale", s)]
    other_pid = other["fresh"][0]["problem_id"]
    assert (await get_problem(db_pool, other_pid))["status"] == "open"


async def test_muted_and_suppressed_findings_are_counted_not_fresh(db_pool):
    s, t = _subject(), _subject()
    first = await _rec(db_pool, [_f(s)])
    await mute_problem(db_pool, first["fresh"][0]["problem_id"], hours=24, by="test", now=NOW)
    await _rec(db_pool, [], now=NOW + timedelta(minutes=1))  # resolve while muted
    await set_service_state(db_pool, t, "deploying", subject_kind="flow", minutes=30, set_by="t", now=NOW)
    out = await _rec(db_pool, [_f(s), _f(t)], now=NOW + timedelta(minutes=2))
    assert out["fresh"] == []
    assert out["muted"] == 1 and out["suppressed"] == 1


async def test_unusable_findings_are_skipped(db_pool):
    out = await _rec(db_pool, [{"klass": "", "subject": ""}, {"klass": "flow_failing"}])
    assert out["fresh"] == [] and out["attached"] == 0


async def test_projection_runs_for_fresh_and_resolved(db_pool, monkeypatch):
    seen: list[str] = []

    async def fake_project(pool, problem_id, **kw):
        seen.append(problem_id)
        return {}

    monkeypatch.setattr("aegis.services.hub_watch.hub_project.project", fake_project)
    s = _subject()
    out = await reconcile_findings(
        db_pool, source=SRC, subject_kind="flow", classes=CLASSES, findings=[_f(s)], now=NOW
    )
    pid = out["fresh"][0]["problem_id"]
    await reconcile_findings(
        db_pool, source=SRC, subject_kind="flow", classes=CLASSES, findings=[],
        now=NOW + timedelta(minutes=1),
    )
    assert seen == [pid, pid]


async def test_projection_failure_does_not_break_the_reconcile(db_pool, monkeypatch):
    async def boom(pool, problem_id, **kw):
        raise RuntimeError("todoist down")

    monkeypatch.setattr("aegis.services.hub_watch.hub_project.project", boom)
    out = await reconcile_findings(
        db_pool, source=SRC, subject_kind="flow", classes=CLASSES, findings=[_f(_subject())], now=NOW
    )
    assert len(out["fresh"]) == 1


def test_mute_hint_names_the_problems():
    assert mute_hint([]) == ""
    hint = mute_hint(["a", "", "b"])
    assert hint.startswith("Silence: UPDATE problems SET muted_until")
    assert "('a', 'b')" in hint
