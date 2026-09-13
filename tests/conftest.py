"""Shared test fixtures for AEGIS v2."""

import os
import signal
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis.config import Settings

from tests import pg_test_db as testdb

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
    if _is_controller(session.config):
        _drop_run_databases(session.config)


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
    # A namespace package has no __file__: nothing to check.
    pkg_path = Path(pkg_file).resolve() if pkg_file else None
    rootdir = Path(str(session.config.rootdir)).resolve()

    if pkg_path is not None and rootdir != pkg_path and rootdir not in pkg_path.parents:
        pytest.exit(
            f"aegis#96 guard: `aegis` package imported from {pkg_path}, which "
            f"is outside the pytest rootdir {rootdir}. This checkout's editable "
            "install resolves to a DIFFERENT clone (likely the main checkout) — "
            "the suite would silently test that code, not this one.\n"
            "Fix: PYTHONPATH=core/src:worker/src:comms/src pytest ...",
            returncode=1,
        )

    if _is_controller(session.config):
        _prepare_run(session.config)


# ---------------------------------------------------------------------------
# aegis#325: one set of test databases per pytest invocation
# ---------------------------------------------------------------------------
#
# Tests get their own databases on the `docker compose` Postgres — never the
# long-lived `aegis` dev database. Names, the run id and the clean-up rules
# live in tests/pg_test_db.py. The old scheme named them after the xdist
# worker alone (`aegis_test_gw0`), so two runs on one host dropped each other's
# databases mid-test and produced hundreds of errors that looked like real
# failures.

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RUN_ID = pytest.StashKey[str]()
# Set when this run must not drop its databases at the end: another run is
# using them (the in-use check below), or the caller asked to keep them.
_KEEP_DATABASES = pytest.StashKey[bool]()
_WORKERINPUT_KEY = "aegis_test_run_id"
_WORKEROUTPUT_KEEP = "aegis_test_keep_databases"


def _is_controller(config: pytest.Config) -> bool:
    """The xdist controller, or the only process of a run without xdist."""
    return not hasattr(config, "workerinput")


def pytest_configure(config: pytest.Config) -> None:
    """Fix this run's id once. Workers take the controller's, never their own."""
    if not _is_controller(config):
        config.stash[_RUN_ID] = config.workerinput[_WORKERINPUT_KEY]
        return
    try:
        config.stash[_RUN_ID] = testdb.run_id()
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from exc


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node) -> None:
    """xdist: hand the controller's run id to each worker before it starts."""
    node.workerinput[_WORKERINPUT_KEY] = node.config.stash[_RUN_ID]


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error) -> None:
    """xdist: a worker that found its database in use says so in its output,
    and the controller must then leave the run's databases alone at the end."""
    if getattr(node, "workeroutput", {}).get(_WORKEROUTPUT_KEEP):
        node.config.stash[_KEEP_DATABASES] = True


def _keep_databases(config: pytest.Config) -> None:
    """Mark this run's databases as not ours to drop, from whichever process
    found out: a worker reports it to the controller, which does the drop."""
    if _is_controller(config):
        config.stash[_KEEP_DATABASES] = True
    else:
        config.workeroutput[_WORKEROUTPUT_KEEP] = True


def _prepare_run(config: pytest.Config) -> None:
    """Controller, before any worker starts: sweep dead runs' databases, and
    stop at once if this run's databases are already in use.

    Best-effort except for the in-use stop: with no Postgres there is nothing
    to sweep, and the database tests skip on their own.
    """
    if os.getenv("TEST_DATABASE_URL"):
        return  # caller-managed database: nothing of ours to create or sweep
    run = config.stash[_RUN_ID]

    async def _housekeeping() -> tuple[dict[str, int], list[str]]:
        conn = await testdb.connect_admin()
        try:
            in_use = await testdb.databases_in_use(conn, run)
            swept = await testdb.sweep_stale(
                conn, my_host=testdb.host_tag(), my_pid=os.getpid()
            )
            return in_use, swept
        finally:
            await conn.close()

    try:
        in_use, swept = testdb.run_sync(_housekeeping)
    except OSError:
        return  # no Postgres reachable
    except Exception as exc:  # never let housekeeping sink the run
        print(f"\n[aegis#325] test-database housekeeping skipped: {exc!r}", file=sys.stderr)
        return
    if swept:
        print(
            f"\n[aegis#325] dropped {len(swept)} test database(s) of dead runs: "
            + ", ".join(swept),
            file=sys.stderr,
        )
    if in_use:
        _keep_databases(config)  # they are someone else's now
        busy = ", ".join(f"{name} ({n} connection(s))" for name, n in sorted(in_use.items()))
        pytest.exit(
            f"aegis#325: this run's test database(s) are already in use: {busy}. "
            f"Another pytest run is using the same run id {run!r} — most likely the "
            f"same {testdb.RUN_ID_ENV}. Unset it to get a private name, or wait for "
            "the other run to finish.",
            returncode=1,
        )


def _drop_run_databases(config: pytest.Config) -> None:
    """Controller, at the very end: drop every database this run created,
    including a crashed worker's. Runs on failure and Ctrl-C too; only a
    killed process skips it, and the next run's sweep covers that."""
    if (
        os.getenv("TEST_DATABASE_URL")
        or os.getenv(testdb.KEEP_ENV)
        or config.stash.get(_KEEP_DATABASES, False)
        or _RUN_ID not in config.stash
    ):
        return
    run = config.stash[_RUN_ID]

    async def _drop() -> None:
        conn = await testdb.connect_admin()
        try:
            await testdb.drop_run_databases(conn, run)
        finally:
            await conn.close()

    try:
        testdb.run_sync(_drop)
    except OSError:
        pass  # no Postgres reachable: nothing was created
    except Exception as exc:
        print(f"\n[aegis#325] could not drop this run's test databases: {exc!r}", file=sys.stderr)


# The `settings` table as migrations + seeds left it, taken when this process
# created its test database: (url, key -> jsonb text). None until then, and
# always None with TEST_DATABASE_URL (a caller-managed database is not ours to
# reset).
_settings_baseline: tuple[str, dict[str, str]] | None = None


@pytest.fixture(scope="session")
def test_db_url(request: pytest.FixtureRequest) -> str | None:
    """URL of a freshly-created, freshly-migrated + seeded session-scoped
    test database.

    `TEST_DATABASE_URL` overrides everything (caller-managed: no drop/create,
    no migrate). Otherwise this process's database (`aegis_test_<run>_<gwN>`,
    see tests/pg_test_db.py) is created, migrated from this checkout's
    migrations/, and seeded from config/seed/ (same as core boot) once per
    session — sharing the dev `aegis` database broke the suite whenever a
    parallel branch applied a divergent migration to it (e.g. the
    maou→finance schema rename).

    Returns None when no Postgres is reachable; db_pool fixtures then skip.
    """
    global _settings_baseline
    override = os.getenv("TEST_DATABASE_URL")
    if override:
        return override
    name = testdb.database_name(
        request.config.stash[_RUN_ID], os.environ.get("PYTEST_XDIST_WORKER")
    )

    async def _prepare() -> tuple[str, dict[str, str]] | int:
        from aegis.db import create_pool, run_migrations
        from aegis.seed import load_seeds

        admin = await testdb.connect_admin()
        try:
            # The name is this run's alone, so a connection to it means another
            # run shares the id (the same AEGIS_TEST_RUN_ID). Dropping it would
            # wreck that run; stop instead.
            busy = await testdb.databases_in_use(admin, request.config.stash[_RUN_ID])
            if busy.get(name):
                return busy[name]
            await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            await admin.execute(f"CREATE DATABASE {name}")
        finally:
            await admin.close()
        url = f"{testdb.PG_SERVER}/{name}"
        pool = await create_pool(url, min_size=1, max_size=2)
        try:
            await run_migrations(pool, _REPO_ROOT / "migrations")
            await load_seeds(pool, _REPO_ROOT / "config" / "seed")
            async with pool.acquire() as conn:
                baseline = await testdb.settings_snapshot(conn)
        finally:
            await pool.close()
        return url, baseline

    try:
        prepared = testdb.run_sync(_prepare)
    except OSError:
        return None
    if isinstance(prepared, int):
        msg = (
            f"aegis#325: test database {name} is already in use ({prepared} "
            "connection(s)) by another pytest run with the same run id "
            f"({testdb.RUN_ID_ENV}). Stopping rather than dropping it under that run."
        )
        _keep_databases(request.config)
        request.session.shouldstop = msg
        pytest.fail(msg, pytrace=False)
    url, baseline = prepared
    _settings_baseline = (url, baseline)
    return url


@pytest.fixture(autouse=True, scope="module")
def _reset_settings_between_files(request: pytest.FixtureRequest):
    """aegis#569: every test file starts from the seeded `settings` table.

    Settings rows are process-wide state in the worker's database, and
    `--dist loadfile` decides which files share a worker — an order that
    changes whenever files are added. A file that leaves a row behind (the
    migration-011 test left the `todoist_capture_enabled` kill switch off)
    silently breaks whichever file lands after it. Resetting at each file
    boundary makes that impossible without touching tests within a file,
    which keep their own order.

    A no-op until this process has created its test database, so files that
    never touch Postgres cost nothing. Set AEGIS_TEST_SETTINGS_LEAKS to a file
    path to have each reset logged there (one line per leaking file).
    AEGIS_TEST_SETTINGS_RESET=off turns the reset off; tests/core/
    test_test_isolation.py uses it to prove a file cleans up after itself.
    """
    yield
    if _settings_baseline is None or os.getenv("AEGIS_TEST_SETTINGS_RESET") == "off":
        return
    url, baseline = _settings_baseline

    async def _restore() -> list[str]:
        import asyncpg

        conn = await asyncpg.connect(url, timeout=5)
        try:
            return await testdb.restore_settings(conn, baseline)
        finally:
            await conn.close()

    try:
        touched = testdb.run_sync(_restore)
    except Exception as exc:
        print(f"\n[aegis#569] could not reset settings after {request.node.nodeid}: {exc!r}",
              file=sys.stderr)
        return
    log = os.getenv("AEGIS_TEST_SETTINGS_LEAKS")
    if touched and log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"{request.node.nodeid}\t{','.join(touched)}\n")

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


@pytest.fixture
def chart():
    """The chart of accounts the money lane reads in these tests — this
    deployment's real one, from `tests/books_chart_data.py` (#560)."""
    from tests.books_chart_data import CHART

    return CHART


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
