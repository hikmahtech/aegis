"""Groups: the same failure on many entities becomes one problem.

Real test database. The judge is not exercised here — it lives in the worker
and its verdict is an input to `upgrade`, which is what these tests pin.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from aegis.services.hub import (
    Event,
    get_problem,
    group_key,
    ingest_event,
    list_events,
)
from aegis.services.hub_group import (
    candidates,
    recent_verdict,
    record_verdict,
    upgrade,
    worst,
)
from aegis.services.hub_watch import reconcile_findings

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def _klass() -> str:
    return f"zzgrp{uuid.uuid4().hex[:8]}"


async def _stuck(pool, klass: str, subject: str, *, now=NOW, kind: str = "post") -> str:
    """One occurrence of `klass` on `subject`; returns its problem id."""
    result = await ingest_event(
        pool,
        Event(
            source="social",
            external_id=f"social:{klass}:{subject}@{now.isoformat()}",
            kind="occurrence",
            title=f"Post {subject} stuck in Postiz",
            subject=subject,
            subject_kind=kind,
            klass=klass,
            occurred_at=now,
        ),
        now=now,
    )
    return result.problem_id


async def _members(pool, klass: str, *subjects: str) -> list[str]:
    """One stuck post per subject, each a second older than the next, so "the
    oldest member becomes the group" names one of them. Seen in the same
    second, the keeper is whichever row the query plan returns first, and that
    changes as the shared test table grows."""
    return [
        await _stuck(pool, klass, s, now=NOW + timedelta(seconds=n)) for n, s in enumerate(subjects)
    ]


# --- the key ----------------------------------------------------------------


def test_group_key_drops_the_subject_and_needs_a_kind():
    assert group_key("stuck_post", "post") == "stuck_post:post"
    # No kind, no group: without one there is no "many entities" to speak of.
    assert group_key("nodedown", "") == ""
    # An unclassified event is the last thing to sweep in with others.
    assert group_key("", "post") == ""
    # A hand-written task's problem is never groupable: three @code tasks are
    # three pieces of work, and folding them would move one task's sessions
    # and PR links onto another.
    assert group_key("manual", "repo") == ""


def test_a_problem_keyed_on_a_task_is_never_groupable():
    """#472 keys a subject-less report about a Todoist task on that task. Each
    is one person's report, like a `manual` problem: three NodeDown tasks are
    three reports, and folding them would close two of the user's tasks."""
    assert group_key("nodedown", "task") == ""


def test_worst_takes_the_most_serious_member():
    assert worst(["warning", "critical", "info"]) == "critical"
    assert worst([]) == "warning"


# --- finding a cluster ------------------------------------------------------


async def test_candidates_finds_one_class_across_several_subjects(db_pool):
    klass = _klass()
    for s in ("a", "b", "c"):
        await _stuck(db_pool, klass, s)
    found = [c for c in await candidates(db_pool, now=NOW) if c["class"] == klass]
    assert len(found) == 1
    assert found[0]["member_count"] == 3
    assert found[0]["group_key"] == f"{klass}:post"
    assert sorted(m["subject"] for m in found[0]["members"]) == ["a", "b", "c"]


async def test_two_of_a_kind_is_not_a_cluster(db_pool):
    klass = _klass()
    for s in ("a", "b"):
        await _stuck(db_pool, klass, s)
    assert [c for c in await candidates(db_pool, now=NOW) if c["class"] == klass] == []


async def test_a_stale_cluster_falls_out_of_the_window(db_pool):
    klass = _klass()
    for s in ("a", "b", "c"):
        await _stuck(db_pool, klass, s, now=NOW - timedelta(days=30))
    assert [c for c in await candidates(db_pool, now=NOW) if c["class"] == klass] == []


# --- the verdict cache ------------------------------------------------------


async def test_a_no_stands_until_the_cluster_grows(db_pool):
    gkey = f"{_klass()}:post"
    await record_verdict(db_pool, gkey, grouped=False, member_count=3, reason="unrelated", now=NOW)
    # same size, still inside the ttl: do not ask again
    assert (await recent_verdict(db_pool, gkey, 3, now=NOW + timedelta(hours=1))) is not None
    # a bigger cluster is a new question
    assert (await recent_verdict(db_pool, gkey, 4, now=NOW + timedelta(hours=1))) is None
    # and the verdict expires
    assert (await recent_verdict(db_pool, gkey, 3, now=NOW + timedelta(days=2))) is None


# --- folding ----------------------------------------------------------------


async def test_upgrade_folds_members_into_one_problem(db_pool):
    klass = _klass()
    ids = await _members(db_pool, klass, "a", "b", "c")
    result = await upgrade(
        db_pool,
        klass=klass,
        subject_kind="post",
        title="3 posts stuck in Postiz",
        member_ids=ids,
        by="test",
        now=NOW,
    )
    # The oldest member is the group and keeps its history.
    assert result["problem_id"] == ids[0]
    group = await get_problem(db_pool, ids[0])
    assert group["group_key"] == f"{klass}:post"
    assert group["subject"] == "*"
    assert group["title"] == "3 posts stuck in Postiz"
    assert group["occurrences"] == 3
    # The others are closed, with a link back.
    for pid in ids[1:]:
        assert (await get_problem(db_pool, pid))["status"] == "closed"
    assert sorted(result["subjects"]) == ["a", "b", "c"]
    # One readable event says why.
    grouped = [
        e
        for e in await list_events(db_pool, ids[0])
        if (e["payload"] or {}).get("action") == "grouped"
    ]
    assert len(grouped) == 1
    assert grouped[0]["payload"]["member_count"] == 3
    # The keeper's own subject was overwritten with `*`; the event keeps it.
    assert grouped[0]["payload"]["members"][0] == "a"


async def test_a_group_absorbs_the_next_one_instead_of_opening_a_task(db_pool):
    klass = _klass()
    ids = await _members(db_pool, klass, "a", "b", "c")
    await upgrade(
        db_pool,
        klass=klass,
        subject_kind="post",
        title="3 posts stuck",
        member_ids=ids,
        by="test",
        now=NOW,
    )
    later = NOW + timedelta(hours=1)
    result = await ingest_event(
        db_pool,
        Event(
            source="social",
            external_id=f"social:{klass}:d@{later.isoformat()}",
            kind="occurrence",
            title="Post d stuck in Postiz",
            subject="d",
            subject_kind="post",
            klass=klass,
            occurred_at=later,
        ),
        now=later,
    )
    assert result.problem_id == ids[0]
    assert result.action == "attached"
    assert result.absorbed is True
    # It never earns its own card, and the timeline can still name the post.
    assert result.investigate is False
    events = await list_events(db_pool, ids[0])
    assert any((e["payload"] or {}).get("member_subject") == "d" for e in events)


async def test_absorption_never_crosses_a_class(db_pool):
    klass, other = _klass(), _klass()
    ids = [await _stuck(db_pool, klass, s) for s in ("a", "b", "c")]
    await upgrade(
        db_pool, klass=klass, subject_kind="post", title="grouped",
        member_ids=ids, by="test", now=NOW,
    )
    fresh = await _stuck(db_pool, other, "d", now=NOW + timedelta(hours=1))
    assert fresh != ids[0]
    assert (await get_problem(db_pool, fresh))["group_key"] is None


async def test_upgrade_is_rerunnable_and_folds_a_left_behind_member(db_pool):
    """A member that existed before the group but was not in the first call —
    it appeared while the judge was thinking — folds into the group that is
    already there rather than starting a second one."""
    klass = _klass()
    ids = await _members(db_pool, klass, "a", "b", "c", "d")
    await upgrade(
        db_pool, klass=klass, subject_kind="post", title="grouped",
        member_ids=ids[:3], by="test", now=NOW,
    )
    assert (await get_problem(db_pool, ids[3]))["status"] == "open"

    result = await upgrade(
        db_pool, klass=klass, subject_kind="post", title="4 posts stuck",
        member_ids=[ids[3]], by="test", now=NOW,
    )
    assert result["problem_id"] == ids[0]
    assert [m["subject"] for m in result["merged"]] == ["d"]
    assert (await get_problem(db_pool, ids[3]))["status"] == "closed"
    group = await get_problem(db_pool, ids[0])
    assert group["title"] == "4 posts stuck"
    assert group["occurrences"] == 4


async def test_upgrade_refuses_a_single_member(db_pool):
    klass = _klass()
    only = await _stuck(db_pool, klass, "a")
    with pytest.raises(ValueError, match="at least two"):
        await upgrade(
            db_pool, klass=klass, subject_kind="post", title="x",
            member_ids=[only], by="test", now=NOW,
        )


async def test_upgrade_refuses_a_kindless_class(db_pool):
    with pytest.raises(ValueError, match="subject kind"):
        await upgrade(
            db_pool, klass="nodedown", subject_kind="", title="x",
            member_ids=["ignored"], by="test", now=NOW,
        )


# --- recovery ---------------------------------------------------------------


async def test_a_group_recovers_only_when_the_whole_class_does(db_pool):
    klass = _klass()

    async def rec(findings, now):
        return await reconcile_findings(
            db_pool,
            source="social",
            subject_kind="post",
            classes=[klass],
            findings=findings,
            now=now,
            project=False,
        )

    def f(subject):
        return {"klass": klass, "subject": subject, "title": f"Post {subject} stuck"}

    await rec([f("a"), f("b"), f("c")], NOW)
    ids = [
        m["id"]
        for c in await candidates(db_pool, now=NOW)
        if c["class"] == klass
        for m in c["members"]
    ]
    group_id = (
        await upgrade(
            db_pool, klass=klass, subject_kind="post", title="3 posts stuck",
            member_ids=ids, by="test", now=NOW,
        )
    )["problem_id"]

    # One post still stuck: the group is NOT recovered, and does not
    # re-resolve every tick just because `*` is not among the findings.
    still = await rec([f("a")], NOW + timedelta(hours=1))
    assert still["resolved"] == []
    assert (await get_problem(db_pool, group_id))["status"] == "open"

    # Nothing stuck: the queue drained, so the group recovers once.
    drained = await rec([], NOW + timedelta(hours=2))
    assert [r["problem_id"] for r in drained["resolved"]] == [group_id]
    assert drained["resolved"][0]["label"] == "3 posts stuck"
    assert (await get_problem(db_pool, group_id))["status"] == "resolved"
    assert (await rec([], NOW + timedelta(hours=3)))["resolved"] == []


async def test_hand_written_tasks_are_never_a_cluster(db_pool):
    """`manual` problems are Todoist tasks, one each. However many are open,
    they must never be offered as a group."""
    from aegis.services.hub_project import ensure_problem_for_task

    await db_pool.execute(
        "INSERT INTO todoist_projects (id, name, is_managed, raw) "
        "VALUES ('P_INBOX','Inbox',true,'{}'::jsonb) ON CONFLICT (id) DO NOTHING"
    )
    for n in range(3):
        task_id = f"zzgrptask{uuid.uuid4().hex[:8]}"
        await db_pool.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, is_completed, raw) "
            "VALUES ($1, 'P_INBOX', $2, false, '{}'::jsonb)",
            task_id,
            f"Fix the thing {n}",
        )
        assert await ensure_problem_for_task(db_pool, task_id) is not None

    assert [c for c in await candidates(db_pool, now=NOW) if c["class"] == "manual"] == []


async def test_task_keyed_reports_are_never_a_cluster(db_pool):
    klass = _klass()
    for _ in range(3):
        await _stuck(db_pool, klass, f"6h{uuid.uuid4().hex[:12]}", kind="task")
    assert [c for c in await candidates(db_pool, now=NOW) if c["class"] == klass] == []


# --- a stray that missed the fold (#474) ---------------------------------------


async def _mirror_task(pool, task_id: str) -> None:
    """The sync mirror row TodoistSyncFlow would have written for a hub task."""
    await pool.execute(
        "INSERT INTO todoist_projects (id, name, is_managed, raw) "
        "VALUES ('P_INBOX','Inbox',true,'{}'::jsonb) ON CONFLICT (id) DO NOTHING"
    )
    await pool.execute(
        "INSERT INTO todoist_tasks (id, project_id, content, labels, assignee_label, "
        "source_tag, is_completed, raw) "
        "VALUES ($1,'P_INBOX','Post d stuck',ARRAY['#alert','@pandora'],'@pandora','#alert',"
        "false,'{}'::jsonb)",
        task_id,
    )


async def _resolve(pool, problem_id: str, now=NOW) -> None:
    await ingest_event(
        pool,
        Event(
            source="social",
            external_id=f"social:resolve:{problem_id}@{now.isoformat()}",
            kind="resolved",
            title="published",
            problem_id=problem_id,
            occurred_at=now,
        ),
        now=now,
    )


async def _group_with_a_stray(pool, klass: str) -> tuple[str, str]:
    """The prod shape behind #474: four posts stuck, one recovers just before
    the sweep folds the other three, then comes back. It reopens its OWN
    problem — its key still holds one — so the group never absorbs it.
    Returns (group id, stray id)."""
    ids = [await _stuck(pool, klass, s) for s in ("a", "b", "c", "d")]
    await _resolve(pool, ids[3])
    group_id = (
        await upgrade(
            pool, klass=klass, subject_kind="post", title="3 posts stuck in Postiz",
            member_ids=ids[:3], by="test", now=NOW,
        )
    )["problem_id"]
    back = await _stuck(pool, klass, "d", now=NOW + timedelta(hours=1))
    assert back == ids[3]
    assert (await get_problem(pool, back))["status"] == "open"
    return group_id, back


async def test_a_stray_that_came_back_after_the_fold_joins_its_group(db_pool):
    """Prod: stuck post 6e92a5fa stayed open beside group a9eca98c — two tasks
    for one condition — because nothing ever looked at a single stray again."""
    from aegis.services.hub_project import link_task

    klass = _klass()
    group_id, stray_id = await _group_with_a_stray(db_pool, klass)
    task_id = f"zzstray{uuid.uuid4().hex[:8]}"
    await _mirror_task(db_pool, task_id)
    assert await link_task(db_pool, stray_id, task_id)

    later = NOW + timedelta(hours=2)
    found = [c for c in await candidates(db_pool, now=later) if c["class"] == klass]
    # One stray is not a cluster to judge: it was folded, deterministically.
    assert found == []
    stray = await get_problem(db_pool, stray_id)
    assert stray["status"] == "closed"
    group = await get_problem(db_pool, group_id)
    assert group["status"] == "open"
    # a, b, c once each, plus d's first sighting and its return.
    assert group["occurrences"] == 5

    grouped = [
        e["payload"]
        for e in await list_events(db_pool, group_id)
        if (e["payload"] or {}).get("action") == "grouped"
        and "d" in ((e["payload"] or {}).get("members") or [])
    ]
    assert len(grouped) == 1
    assert grouped[0]["by"] == "hub-sweep"
    assert grouped[0]["reason"]

    # Its task is retired through the outbox: a note saying where the work
    # went, then the completion.
    queued = [
        r["command"]
        for r in await db_pool.fetch(
            "SELECT command FROM todoist_outbox WHERE command->'args'->>'id' = $1 "
            "   OR command->'args'->>'item_id' = $1 ORDER BY id",
            task_id,
        )
    ]
    assert [c["type"] for c in queued] == ["note_add", "item_complete"]
    assert "Workflow run:" in queued[0]["args"]["content"]
    assert await db_pool.fetchval(
        "SELECT is_completed FROM todoist_tasks WHERE id = $1", task_id
    )


async def test_a_stray_sharing_the_groups_task_leaves_that_task_open(db_pool):
    """#472 could link one task to two problems. Folding the second must not
    close the task the group itself still needs."""
    from aegis.services.hub_project import link_task

    klass = _klass()
    group_id, stray_id = await _group_with_a_stray(db_pool, klass)
    task_id = f"zzshared{uuid.uuid4().hex[:8]}"
    await _mirror_task(db_pool, task_id)
    assert await link_task(db_pool, group_id, task_id)
    assert await link_task(db_pool, stray_id, task_id)

    await candidates(db_pool, now=NOW + timedelta(hours=2))
    assert (await get_problem(db_pool, stray_id))["status"] == "closed"
    assert not await db_pool.fetchval(
        "SELECT is_completed FROM todoist_tasks WHERE id = $1", task_id
    )
    assert not await db_pool.fetchval(
        "SELECT count(*) FROM todoist_outbox WHERE command->'args'->>'id' = $1 "
        "   OR command->'args'->>'item_id' = $1",
        task_id,
    )


async def test_absorbing_a_stray_twice_counts_it_once(db_pool):
    klass = _klass()
    group_id, _ = await _group_with_a_stray(db_pool, klass)
    later = NOW + timedelta(hours=2)
    await candidates(db_pool, now=later)
    await candidates(db_pool, now=later + timedelta(minutes=5))
    assert (await get_problem(db_pool, group_id))["occurrences"] == 5


async def test_a_stray_is_never_folded_into_a_resolved_group(db_pool):
    """Folding a live problem into a resolved one would hide it: the merge
    keeps the group's status."""
    klass = _klass()
    group_id, stray_id = await _group_with_a_stray(db_pool, klass)
    await _resolve(db_pool, group_id, NOW + timedelta(hours=1))
    await candidates(db_pool, now=NOW + timedelta(hours=2))
    assert (await get_problem(db_pool, stray_id))["status"] == "open"


async def test_a_stray_outside_the_window_is_left_alone(db_pool):
    """The same window the judge's candidates use: last month's problem is not
    evidence about today's condition."""
    klass = _klass()
    old = await _stuck(db_pool, klass, "old", now=NOW - timedelta(days=10))
    ids = [await _stuck(db_pool, klass, s) for s in ("a", "b", "c")]
    await upgrade(
        db_pool, klass=klass, subject_kind="post", title="3 posts stuck",
        member_ids=ids, by="test", now=NOW,
    )
    await candidates(db_pool, now=NOW + timedelta(hours=1))
    assert (await get_problem(db_pool, old))["status"] == "open"


async def test_absorption_never_crosses_a_class_or_a_kind(db_pool):
    klass, other = _klass(), _klass()
    group_id, _ = await _group_with_a_stray(db_pool, klass)
    other_class = await _stuck(db_pool, other, "d", now=NOW + timedelta(hours=1))
    other_kind = await _stuck(db_pool, klass, "e", now=NOW + timedelta(hours=1), kind="queue")
    await candidates(db_pool, now=NOW + timedelta(hours=2))
    assert (await get_problem(db_pool, other_class))["status"] == "open"
    assert (await get_problem(db_pool, other_kind))["status"] == "open"
    assert (await get_problem(db_pool, other_kind))["group_key"] is None


async def test_a_money_stray_is_never_folded(db_pool):
    """The money lane's findings are one problem per account by construction
    (`NON_GROUPABLE_SOURCES`); the absorption keeps the same fence the judge's
    candidates do, even when a group of the same class and kind exists."""
    from aegis.services import statement_findings as sf

    klass = _klass()
    subjects = [f"zzacct-{uuid.uuid4().hex[:8]}" for _ in range(3)]
    await reconcile_findings(
        db_pool,
        source="flow_health",
        subject_kind="instrument",
        classes=[klass],
        findings=[{"klass": klass, "subject": s, "title": f"{klass}: {s}"} for s in subjects],
        now=NOW,
        project=False,
    )
    # The money finding exists BEFORE the group, as the prod stray did: once a
    # group exists, a new key is absorbed at ingest and never becomes a stray.
    money = await ingest_event(
        db_pool,
        Event(
            source=sf.SOURCE,
            external_id=f"money:{klass}:zzmoney@{NOW.isoformat()}",
            kind="occurrence",
            title="unmatched rows",
            subject=f"zzmoney-{uuid.uuid4().hex[:8]}",
            subject_kind="instrument",
            klass=klass,
            occurred_at=NOW,
        ),
        now=NOW,
    )
    members = [
        m["id"]
        for c in await candidates(db_pool, now=NOW)
        if c["class"] == klass
        for m in c["members"]
    ]
    assert money.problem_id not in members
    await upgrade(
        db_pool, klass=klass, subject_kind="instrument", title="3 flows",
        member_ids=members, by="test", now=NOW,
    )
    await candidates(db_pool, now=NOW + timedelta(hours=1))
    assert (await get_problem(db_pool, money.problem_id))["status"] == "open"


async def test_a_non_groupable_source_is_never_offered_as_a_cluster(db_pool):
    """Money's findings are already one problem per account by construction, so
    folding three accounts into one `unmatched_rows:instrument:*` problem would
    replace the whole point of the lane with a single task — and `candidates`
    clusters on (class, subject_kind) alone, so it would happen on the next
    sweep tick without the money lane doing anything at all."""
    from aegis.services import statement_findings as sf

    instruments = [f"zzacct-{uuid.uuid4().hex[:8]}" for _ in range(3)]
    await reconcile_findings(
        db_pool,
        source=sf.SOURCE,
        subject_kind=sf.INSTRUMENT,
        classes=list(sf.INSTRUMENT_CLASSES),
        findings=[
            {
                "klass": sf.UNMATCHED_ROWS,
                "subject": i,
                "title": f"3 unmatched rows on {i}",
            }
            for i in instruments
        ],
        now=NOW,
        project=False,
    )
    mine = set(instruments)
    for cluster in await candidates(db_pool, now=NOW):
        assert not (mine & {m["subject"] for m in cluster["members"]}), cluster["class"]


async def test_the_same_shape_from_another_source_still_clusters(db_pool):
    """The control for the test above: the exclusion is by SOURCE, not by
    shape. Three problems of one class on three instruments from a watchdog
    that IS groupable are still offered."""
    klass = _klass()
    subjects = [f"zzacct-{uuid.uuid4().hex[:8]}" for _ in range(3)]
    await reconcile_findings(
        db_pool,
        source="flow_health",
        subject_kind="instrument",
        classes=[klass],
        findings=[{"klass": klass, "subject": s, "title": f"{klass}: {s}"} for s in subjects],
        now=NOW,
        project=False,
    )
    found = [c for c in await candidates(db_pool, now=NOW) if c["class"] == klass]
    assert len(found) == 1
    assert sorted(m["subject"] for m in found[0]["members"]) == sorted(subjects)


async def test_a_group_that_rolls_over_is_still_a_group(db_pool):
    """The group resolved long ago and the condition is back. That starts a
    fresh problem — and it must still be the group, or the sweep would have to
    pay to re-judge a cluster somebody already ruled on."""
    klass = _klass()
    ids = [await _stuck(db_pool, klass, s) for s in ("a", "b", "c")]
    first = (
        await upgrade(
            db_pool, klass=klass, subject_kind="post", title="3 posts stuck",
            member_ids=ids, by="test", now=NOW,
        )
    )["problem_id"]

    # It recovers…
    await ingest_event(
        db_pool,
        Event(
            source="social",
            external_id=f"social:{klass}:*@{NOW.isoformat()}@resolved",
            kind="resolved",
            title="drained",
            problem_id=first,
            occurred_at=NOW,
        ),
        now=NOW,
    )
    assert (await get_problem(db_pool, first))["status"] == "resolved"

    # …and comes back a week later, past the reopen window.
    later = NOW + timedelta(days=7)
    result = await ingest_event(
        db_pool,
        Event(
            source="social",
            external_id=f"social:{klass}:e@{later.isoformat()}",
            kind="occurrence",
            title="Post e stuck in Postiz",
            subject="e",
            subject_kind="post",
            klass=klass,
            occurred_at=later,
        ),
        now=later,
    )
    assert result.action == "rolled_over"
    assert result.problem_id != first
    fresh = await get_problem(db_pool, result.problem_id)
    assert fresh["group_key"] == f"{klass}:post"
    assert fresh["subject"] == "*"
    assert fresh["title"] == "3 posts stuck"
    assert (await get_problem(db_pool, first))["status"] == "closed"
