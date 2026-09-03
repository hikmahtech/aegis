"""CleanupActivities.cleanup_task_sessions — the finished-session worktree sweep.

Runs against the real test database, because the whole activity IS a predicate:
"task completed or gone, AND idle longer than N days". A mocked pool would let
that predicate be anything at all and still pass.

The connector is faked, but its signature is pinned to the real
`RemoteScriptConnector.remove_worktree` at the bottom of this file — a fake that
has drifted from the class it stands in for is the one way these tests could
pass while production is broken.
"""

from __future__ import annotations

import inspect

import pytest
import pytest_asyncio
from aegis.connectors.remote_script import RemoteScriptConnector
from aegis.services import task_sessions as svc
from aegis_worker.activities.cleanup import CleanupActivities
from temporalio.testing import ActivityEnvironment

_AGENT = "zzsweep-agent"
_DONE = "zzsweep-done"
_OPEN = "zzsweep-open"
_YOUNG = "zzsweep-young"
_ORPHAN = "zzsweep-orphan"
_ALL = (_DONE, _OPEN, _YOUNG, _ORPHAN)

_WT = "/w/hikmah/aegis-aegis-wt/task-zzsweep"
_HOST = "coding-host"


class _FakeRemoteScript:
    """Records every removal request; never raises, like the real connector."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def remove_worktree(self, worktree_path: str, host: str = "") -> None:
        self.calls.append((worktree_path, host))


class _RaisingRemoteScript:
    """A connector that has itself broken — SSH down, config unreadable."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def remove_worktree(self, worktree_path: str, host: str = "") -> None:
        self.calls.append((worktree_path, host))
        raise RuntimeError("ssh: connect to host coding-host port 22: No route to host")


async def _purge(pool) -> None:
    await pool.execute("DELETE FROM task_sessions WHERE task_id = ANY($1::text[])", list(_ALL))
    await pool.execute("DELETE FROM todoist_tasks WHERE id = ANY($1::text[])", list(_ALL))


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await _purge(db_pool)
    yield db_pool
    await _purge(db_pool)


async def _task(pool, task_id: str, *, completed: bool) -> None:
    await pool.execute(
        "INSERT INTO todoist_tasks (id, content, description, labels, source_tag, "
        "assignee_label, is_completed, updated_at) "
        "VALUES ($1, 'Fix the retry policy', '', ARRAY['@pandora','@code'], NULL, "
        "'@pandora', $2, now())",
        task_id,
        completed,
    )


async def _session(
    pool,
    task_id: str,
    *,
    age: str,
    worktree_path: str = _WT,
    host: str = _HOST,
) -> None:
    """A session row whose last activity is `age` old (a Postgres interval)."""
    await svc.create_session(pool, task_id=task_id, agent_id=_AGENT)
    if worktree_path:
        await svc.set_repo(
            pool,
            task_id,
            repo="hikmah/aegis",
            github_repo="hikmahtech/aegis",
            worktree_path=worktree_path,
            branch=f"aegis-task/{task_id}",
            host=host,
        )
    # $2::text::interval, not $2::interval — a bare interval cast makes asyncpg
    # infer the parameter as an interval and demand a timedelta.
    await pool.execute(
        "UPDATE task_sessions SET last_turn_at = now() - $2::text::interval, "
        "created_at = now() - $2::text::interval WHERE task_id = $1",
        task_id,
        age,
    )


async def _exists(pool, task_id: str) -> bool:
    return bool(
        await pool.fetchval("SELECT 1 FROM task_sessions WHERE task_id = $1", task_id)
    )


async def _sweep(pool, connector, days: int = 7) -> dict:
    acts = CleanupActivities(db_pool=pool, remote_script=connector)
    return await ActivityEnvironment().run(acts.cleanup_task_sessions, days)


# ── the sweep itself ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aged_completed_task_loses_its_worktree_and_row(pool):
    """The whole point: a finished task idle past the window is cleaned up, and
    the worktree removal is aimed at the row's own path AND its own host."""
    await _task(pool, _DONE, completed=True)
    await _session(pool, _DONE, age="30 days")
    rs = _FakeRemoteScript()

    result = await _sweep(pool, rs)

    assert result == {"removed": 1, "skipped": 0}
    assert rs.calls == [(_WT, _HOST)]
    assert not await _exists(pool, _DONE)


@pytest.mark.asyncio
async def test_open_task_row_survives(pool):
    """An OPEN task is live work no matter how long it has been idle. Deleting
    its row would orphan the worktree an operator may be sitting in."""
    await _task(pool, _OPEN, completed=False)
    await _session(pool, _OPEN, age="365 days")
    rs = _FakeRemoteScript()

    result = await _sweep(pool, rs)

    assert result == {"removed": 0, "skipped": 0}
    assert rs.calls == []
    assert await _exists(pool, _OPEN)


@pytest.mark.asyncio
async def test_recently_active_completed_task_survives(pool):
    """Completing a task does not mean the work is over — a PR may still be in
    review in that worktree. Only idleness past the window releases it."""
    await _task(pool, _YOUNG, completed=True)
    await _session(pool, _YOUNG, age="1 day")
    rs = _FakeRemoteScript()

    result = await _sweep(pool, rs)

    assert result == {"removed": 0, "skipped": 0}
    assert rs.calls == []
    assert await _exists(pool, _YOUNG)


@pytest.mark.asyncio
async def test_task_gone_from_todoist_is_swept(pool):
    """A deleted task never comes back as `is_completed`; it simply stops
    existing. Without the LEFT JOIN arm those worktrees would live forever."""
    await _session(pool, _ORPHAN, age="30 days")  # no todoist_tasks row at all
    rs = _FakeRemoteScript()

    result = await _sweep(pool, rs)

    assert result == {"removed": 1, "skipped": 0}
    assert rs.calls == [(_WT, _HOST)]
    assert not await _exists(pool, _ORPHAN)


@pytest.mark.asyncio
async def test_only_the_eligible_row_is_touched(pool):
    """All four states in one sweep: the two eligible rows go, the two live
    ones stay. Guards against a predicate that is right row-by-row but wrong
    when the table holds more than the row under test."""
    await _task(pool, _DONE, completed=True)
    await _session(pool, _DONE, age="30 days")
    await _task(pool, _OPEN, completed=False)
    await _session(pool, _OPEN, age="30 days")
    await _task(pool, _YOUNG, completed=True)
    await _session(pool, _YOUNG, age="2 hours")
    await _session(pool, _ORPHAN, age="30 days")
    rs = _FakeRemoteScript()

    result = await _sweep(pool, rs)

    assert result == {"removed": 2, "skipped": 0}
    assert sorted(rs.calls) == sorted([(_WT, _HOST), (_WT, _HOST)])
    assert not await _exists(pool, _DONE)
    assert not await _exists(pool, _ORPHAN)
    assert await _exists(pool, _OPEN)
    assert await _exists(pool, _YOUNG)


@pytest.mark.asyncio
async def test_days_argument_moves_the_window(pool):
    """`days` is honoured, not hardcoded: a 1-day-idle row is swept at days=1
    and kept at the default 7."""
    await _task(pool, _DONE, completed=True)
    await _session(pool, _DONE, age="2 days")

    kept = await _sweep(pool, _FakeRemoteScript(), 7)
    assert kept == {"removed": 0, "skipped": 0}
    assert await _exists(pool, _DONE)

    swept = await _sweep(pool, _FakeRemoteScript(), 1)
    assert swept == {"removed": 1, "skipped": 0}
    assert not await _exists(pool, _DONE)


# ── failure handling: the row is the retry token ────────────────────────────


@pytest.mark.asyncio
async def test_raising_connector_keeps_the_row_and_counts_skipped(pool):
    """`remove_worktree` is documented never to raise, so a raise means the
    connector itself is broken. Deleting the row anyway would strand the
    worktree forever — nothing else knows the path. Keep it; retry tomorrow."""
    await _task(pool, _DONE, completed=True)
    await _session(pool, _DONE, age="30 days")
    rs = _RaisingRemoteScript()

    result = await _sweep(pool, rs)

    assert result == {"removed": 0, "skipped": 1}
    assert rs.calls == [(_WT, _HOST)]
    assert await _exists(pool, _DONE)


@pytest.mark.asyncio
async def test_no_connector_skips_a_row_that_has_a_worktree(pool):
    """Same reasoning with no connector wired at all: the path is still real."""
    await _task(pool, _DONE, completed=True)
    await _session(pool, _DONE, age="30 days")

    result = await _sweep(pool, None)

    assert result == {"removed": 0, "skipped": 1}
    assert await _exists(pool, _DONE)


@pytest.mark.asyncio
async def test_row_without_a_worktree_is_deleted_with_no_connector(pool):
    """A session that never resolved a repo has nothing on disk, so there is
    nothing to strand — the bookkeeping row goes regardless of the connector."""
    await _task(pool, _DONE, completed=True)
    await _session(pool, _DONE, age="30 days", worktree_path="", host="")

    result = await _sweep(pool, None)

    assert result == {"removed": 1, "skipped": 0}
    assert not await _exists(pool, _DONE)


# ── guards ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_pool_returns_zeroes():
    acts = CleanupActivities(db_pool=None, remote_script=_FakeRemoteScript())
    result = await ActivityEnvironment().run(acts.cleanup_task_sessions, 7)
    assert result == {"removed": 0, "skipped": 0}


@pytest.mark.asyncio
async def test_non_positive_days_disables_the_sweep(pool):
    """0 is the caller's opt-out; a negative window would sweep the future."""
    await _task(pool, _DONE, completed=True)
    await _session(pool, _DONE, age="30 days")
    rs = _FakeRemoteScript()

    assert await _sweep(pool, rs, 0) == {"removed": 0, "skipped": 0}
    assert await _sweep(pool, rs, -3) == {"removed": 0, "skipped": 0}
    assert rs.calls == []
    assert await _exists(pool, _DONE)


# ── the fakes must match the class they stand in for ────────────────────────


def test_fake_connector_signatures_match_the_real_one():
    real = inspect.signature(RemoteScriptConnector.remove_worktree)
    for fake in (_FakeRemoteScript, _RaisingRemoteScript):
        assert inspect.signature(fake.remove_worktree) == real, fake.__name__
