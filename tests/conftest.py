"""Shared test fixtures for AEGIS v2."""

import asyncio
import os
import signal
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis.config import Settings

# ---------------------------------------------------------------------------
# aegis#190: never leave an orphaned Temporal ephemeral server behind
# ---------------------------------------------------------------------------
#
# `WorkflowEnvironment.start_time_skipping()` / `.start_local()` spawn a server
# binary as a DIRECT CHILD of this pytest process, and only kill it from
# `WorkflowEnvironment.__aexit__`. Every one of our ~48 flow-test modules drives
# that via an in-test `async with`, so the shutdown depends on the test
# coroutine unwinding — which is exactly what does NOT happen when pytest-timeout
# fires: its SIGALRM handler raises `Failed` from whatever frame the main thread
# happens to be in (normally `selectors.select()` deep inside the event loop),
# so the exception leaves `run_until_complete` without ever resuming the test
# coroutine. The `async with` body is abandoned mid-await; when the coroutine is
# finally garbage-collected the loop is gone and `__aexit__` dies with
# "RuntimeError: no running event loop". The server survives — a hung next run,
# and (locally) a permanently held `flock` fd, since the child inherits it.
#
# So: reap our own leaked children after every test and at session end, and
# reap pre-existing orphans at session start. Linux-only (/proc); a no-op
# everywhere else and a no-op whenever there is nothing to reap.

# The SDK downloads its ephemeral servers to the system temp dir as
# "<binary>-<sdk-name>-<sdk-version>" — /tmp/temporal-test-server-sdk-python-1.30.0
# for start_time_skipping(), /tmp/temporal-sdk-python-1.30.0 for start_local().
# Matching the full prefix keeps a developer's own `temporal` CLI (basename
# "temporal") and every other process on the box out of scope.
_EPHEMERAL_SERVER_PREFIXES = ("temporal-test-server-sdk-", "temporal-sdk-")
_PROC = Path("/proc")


def _is_ephemeral_temporal_server(pid: int) -> bool:
    """True iff `pid` is one of the temporalio SDK's downloaded server binaries.

    Reading /proc/<pid>/exe requires the process to be ours, so this also
    implicitly scopes the match to the current user.
    """
    try:
        exe = os.readlink(_PROC / str(pid) / "exe")
    except OSError:
        return False  # gone, another user's, or a kernel thread
    # A deleted binary reads back as "<path> (deleted)"; the prefix still matches.
    return os.path.basename(exe).startswith(_EPHEMERAL_SERVER_PREFIXES)


def _proc_field(pid: int, name: str) -> str:
    try:
        for line in (_PROC / str(pid) / "status").read_text().splitlines():
            if line.startswith(name):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def _is_gone(pid: int) -> bool:
    """True once `pid` has exited. `os.kill(pid, 0)` is NOT usable here: our own
    killed children stay around as zombies until waited on, and a zombie still
    accepts signal 0. So reap opportunistically and read the /proc state."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except OSError:
        pass  # not our child — nothing to reap
    state = _proc_field(pid, "State:")
    return not state or state.startswith("Z")


def _kill_pids(pids: list[int]) -> None:
    """SIGTERM, then SIGKILL anything still alive after a grace period."""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    remaining = list(pids)
    deadline = time.monotonic() + 5.0
    while remaining and time.monotonic() < deadline:
        remaining = [pid for pid in remaining if not _is_gone(pid)]
        if remaining:
            time.sleep(0.02)
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _all_server_pids() -> list[int]:
    """Every ephemeral Temporal server process visible to this user."""
    try:
        entries = os.listdir(_PROC)
    except OSError:
        return []
    return sorted(
        pid
        for pid in (int(e) for e in entries if e.isdigit())
        if _is_ephemeral_temporal_server(pid)
    )


def _own_leaked_servers() -> list[int]:
    """Ephemeral Temporal servers that are children of THIS pytest process.

    Parentage is the whole scoping argument: we started it, so it is ours to
    kill. Children are collected per-thread because the SDK's Rust runtime may
    fork the server off a worker thread rather than the main one.
    """
    if sys.platform != "linux":
        return []
    me = os.getpid()
    tasks = _PROC / str(me) / "task"
    if not (tasks / str(me) / "children").exists():
        # Kernel without CONFIG_PROC_CHILDREN — fall back to a full scan.
        return [pid for pid in _all_server_pids() if _proc_field(pid, "PPid:") == str(me)]
    children: set[int] = set()
    try:
        for tid in os.listdir(tasks):
            try:
                children.update(int(p) for p in (tasks / tid / "children").read_text().split())
            except OSError:
                continue
    except OSError:
        return []
    return sorted(p for p in children if _is_ephemeral_temporal_server(p))


def _orphaned_servers() -> list[int]:
    """Ephemeral Temporal servers left behind by a PREVIOUS pytest process.

    Deliberately narrow, because killing the wrong process is worse than the
    bug: the executable must be one of the SDK's downloaded server binaries
    (which also proves it is ours to read), and its parent must no longer be a
    live Python process. A server belonging to a pytest session that is still
    running — another agent's, or a sibling xdist worker's — has a live `python`
    parent and is skipped.
    """
    if sys.platform != "linux":
        return []
    orphans = []
    for pid in _all_server_pids():
        ppid = _proc_field(pid, "PPid:")
        parent_comm = _proc_field(int(ppid), "Name:") if ppid.isdigit() else ""
        # Reparented to init/systemd (or anything that is not an interpreter)
        # ⇒ whoever started it is gone.
        if not parent_comm.startswith(("python", "pytest")):
            orphans.append(pid)
    return orphans


def _reap_temporal_servers(pids: list[int], why: str) -> None:
    if not pids:
        return  # the common case: nothing to do, nothing printed
    print(f"\n[aegis#190] reaping {why} temporal test server(s): {pids}", file=sys.stderr)
    _kill_pids(pids)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item):
    """Kill any ephemeral server this test left running.

    A wrapper, not a plain hook, for two reasons. It must run AFTER the fixture
    finalizers — the handful of modules that own their WorkflowEnvironment in a
    `pytest_asyncio.fixture` shut it down there, and reaping first turns their
    teardown into `RuntimeError: Failed shutting down Temporalite: No such
    process`. And the post-yield half runs from a `finally`, so a teardown that
    itself blows up still gets the server cleaned up.

    pytest always runs the teardown phase, including after a failure, a setup
    error, or a pytest-timeout kill, which makes this the backstop for the
    abandoned-`async with` path described above. Every WorkflowEnvironment in
    this suite is function-scoped, so a server still alive here is by definition
    a leak.
    """
    try:
        return (yield)
    finally:
        _reap_temporal_servers(_own_leaked_servers(), "leaked")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    _reap_temporal_servers(_own_leaked_servers(), "leaked")


def pytest_sessionstart(session: pytest.Session) -> None:
    """Reap aegis#190 orphans, then guard against aegis#96.

    aegis#96: wrong-checkout tests running silently green (or red).

    This repo's `.venv` is built from editable installs
    (`pip install -e core -e worker -e comms`), so the interpreter's
    site-packages `.pth` entries point at wherever that `pip install` was run
    from — normally the main checkout. A bare `pytest` invoked from a git
    worktree still finds those `.pth` entries first and imports `aegis` (and
    `aegis_worker` / `aegis_comms`) from the MAIN checkout's `core/src/`, not
    from the worktree whose tests are being collected. The suite then
    exercises unmodified main code while running the worktree's test files —
    a false green for the change under test, or a confusing false red.

    Detect it here rather than let it happen quietly: the already-imported
    `aegis` package must resolve to a path under this session's rootdir.
    """
    # A server orphaned by an earlier run keeps its port, its Postgres/test-lock
    # fds and its worker registrations — enough to wedge this session before it
    # gets anywhere. Clear it out first.
    _reap_temporal_servers(_orphaned_servers(), "orphaned")

    import aegis

    pkg_file = getattr(aegis, "__file__", None)
    if not pkg_file:
        return  # namespace package with no __file__: nothing to check
    pkg_path = Path(pkg_file).resolve()
    rootdir = Path(str(session.config.rootdir)).resolve()

    if rootdir != pkg_path and rootdir not in pkg_path.parents:
        pytest.exit(
            f"aegis#96 guard: `aegis` package imported from {pkg_path}, which "
            f"is outside the pytest rootdir {rootdir}. This checkout's editable "
            "install resolves to a DIFFERENT clone (likely the main checkout) — "
            "the suite would silently test that code, not this one.\n"
            "Fix: PYTHONPATH=core/src:worker/src:comms/src pytest ...",
            returncode=1,
        )

# Postgres server from `docker compose up -d postgres`. Tests get their own
# database on it (below) — never the long-lived `aegis` dev database.
_PG_SERVER = "postgresql://aegis:aegis_dev@localhost:25432"
# One database PER xdist worker. Every worker is its own pytest session and so
# runs this fixture independently — sharing a name means worker B drops and
# recreates the database worker A is mid-test on. Plain (non-xdist) runs have
# no PYTEST_XDIST_WORKER and keep the historical `aegis_test`.
_TEST_DB = "aegis_test" + (
    f"_{os.environ['PYTEST_XDIST_WORKER']}" if os.environ.get("PYTEST_XDIST_WORKER") else ""
)
_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def test_db_url() -> str | None:
    """URL of a freshly-created, freshly-migrated + seeded session-scoped
    test database.

    `TEST_DATABASE_URL` overrides everything (caller-managed: no drop/create,
    no migrate). Otherwise `aegis_test` is dropped, recreated, migrated from
    this checkout's migrations/, and seeded from config/seed/ (same as core
    boot) once per session — sharing the dev `aegis` database broke the suite
    whenever a parallel branch applied a divergent migration to it (e.g. the
    maou→finance schema rename).

    Returns None when no Postgres is reachable; db_pool fixtures then skip.
    """
    override = os.getenv("TEST_DATABASE_URL")
    if override:
        return override

    async def _prepare() -> str:
        import asyncpg
        from aegis.db import create_pool, run_migrations
        from aegis.seed import load_seeds

        admin = await asyncpg.connect(f"{_PG_SERVER}/aegis")
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {_TEST_DB} WITH (FORCE)")
            await admin.execute(f"CREATE DATABASE {_TEST_DB}")
        finally:
            await admin.close()
        url = f"{_PG_SERVER}/{_TEST_DB}"
        pool = await create_pool(url, min_size=1, max_size=2)
        try:
            await run_migrations(pool, _REPO_ROOT / "migrations")
            await load_seeds(pool, _REPO_ROOT / "config" / "seed")
        finally:
            await pool.close()
        return url

    try:
        return asyncio.run(_prepare())
    except OSError:
        return None

# Defaults for Settings fields that are now REQUIRED (no production default)
# but still need a value to instantiate the model in tests.
_TEST_REQUIRED_SETTINGS: dict = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "n8n_ui_url": "https://n8n.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
    "n8n_webhook_secret": "test-secret",
}


@pytest.fixture
def test_settings() -> Settings:
    """Settings with test-safe defaults."""
    return Settings(**_TEST_REQUIRED_SETTINGS)


def _make_pool_acquire(fetchval_return=None):
    """Return a MagicMock for pool.acquire() that supports `async with pool.acquire() as conn`.

    tier.resolve_model_for_agent uses this pattern. By default fetchval returns None,
    which makes the tier resolver fall back to 'balanced'.
    """
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=fetchval_return)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


@pytest.fixture
def mock_db_pool():
    """Mock asyncpg pool."""
    pool = AsyncMock()
    pool.fetchval.return_value = 1
    pool.fetch.return_value = []
    pool.fetchrow.return_value = None
    pool.execute.return_value = "OK"
    pool.close = AsyncMock()
    # Support `async with pool.acquire() as conn:` used by resolve_model_for_agent.
    # Returns None from fetchval → tier resolver falls back to 'balanced'.
    pool.acquire = _make_pool_acquire(fetchval_return=None)
    return pool


@pytest.fixture(autouse=True, scope="session")
def _load_model_tiers_for_tests() -> None:
    """Ensure the tier map is populated for all tests.

    Tests that use send_message (or resolve_model_for_agent) need _TIERS populated
    or the 'balanced' fallback will KeyError. This fixture sets a minimal map once
    per session so all tests start with a working tier resolver.
    """
    from aegis.llm.tier import set_model_tiers

    set_model_tiers({"fast": "gemma4:e2b", "balanced": "qwen3:14b", "smart": "qwen3:32b"})


@pytest.fixture(autouse=True)
def _isolate_model_tiers():
    """aegis#250: `set_model_tiers` installs a process-global map, so a test that
    installs a real backend map (e.g. a worker test exercising llm_backend) leaks
    it into whatever test runs next in the same interpreter. Invisible in CI,
    which never mixes core and worker files in one process, but real for a
    developer validating a cross-package change with `-n 1`. Snapshot/restore
    the map around every test so installation can't escape the test that did it."""
    import aegis.llm.tier as _tier_mod

    saved = dict(_tier_mod._TIERS)
    yield
    _tier_mod._TIERS.clear()
    _tier_mod._TIERS.update(saved)
