"""Test-database names, clean-up and the settings baseline (#325, #569).

Every pytest invocation gets its own databases on the shared Postgres server,
so two runs on one host (normal here: several agents run tests at once) can no
longer drop each other's databases mid-test.

Names:

- ``aegis_test_<run_id>_<gwN>`` for an xdist worker,
- ``aegis_test_<run_id>`` for a run without xdist.

``run_id`` is ``<controller pid>_<host tag>`` (e.g. ``812345_3fa9c1``). The
controller makes it once and hands it to its workers through xdist's
``workerinput``, so all of one run's databases share it and no two live runs
can: a pid is unique among the processes alive on one host, and the host tag
tells hosts (and pid namespaces) apart. ``AEGIS_TEST_RUN_ID`` overrides it with
a fixed name; two runs that set the same value collide on purpose.

A run drops its own databases when it finishes. A run that was killed leaves
them behind, so every run starts by sweeping databases whose run is provably
dead: see ``is_stale``.

This module is imported by ``tests/conftest.py``, so it is an input of every
test job — the workflows' ``paths:`` filters list it (#170).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import socket
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor

import asyncpg

# Postgres server from `docker compose up -d postgres` (and CI's service).
PG_SERVER = "postgresql://aegis:aegis_dev@localhost:25432"
ADMIN_URL = f"{PG_SERVER}/aegis"

RUN_ID_ENV = "AEGIS_TEST_RUN_ID"
KEEP_ENV = "AEGIS_TEST_KEEP_DB"
PREFIX = "aegis_test_"

# An override is letters and digits only. No underscore means it can never
# look like a generated id (`<pid>_<tag>`), so the stale sweep never touches
# it, and it can never end in `_gwN` and be mistaken for a worker database.
_OVERRIDE = re.compile(r"^[a-z0-9]{1,24}$")
# A generated database name. Old-style names (`aegis_test`, `aegis_test_gwN`)
# do not match, so the sweep leaves checkouts still on the old code alone.
_GENERATED = re.compile(r"^aegis_test_(\d+)_([0-9a-f]{6})(?:_gw\d+)?$")
_GENERATED_SQL = r"^aegis_test_[0-9]+_[0-9a-f]{6}(_gw[0-9]+)?$"


def host_tag() -> str:
    """Six hex characters naming this host and pid namespace.

    The pid in a run id only means something on the host (and inside the pid
    namespace) that issued it. Hostname alone is not enough: a container run
    with `--network host` shares the host's name but not its pids.
    """
    ident = socket.gethostname()
    try:
        ident += "|" + os.readlink("/proc/self/ns/pid")
    except OSError:
        pass  # not Linux: the hostname is all there is
    return hashlib.sha256(ident.encode()).hexdigest()[:6]


def run_id() -> str:
    """This run's id: the `AEGIS_TEST_RUN_ID` override, else `<pid>_<host tag>`.

    Call it in the controller only. Workers must use the id the controller
    hands them, or each would name its databases after its own pid.
    """
    override = os.environ.get(RUN_ID_ENV, "")
    if override:
        if not _OVERRIDE.match(override):
            raise ValueError(
                f"{RUN_ID_ENV}={override!r} is not a valid test run id: use 1-24 "
                "lowercase letters and digits (no underscores)."
            )
        return override
    return f"{os.getpid()}_{host_tag()}"


def database_name(run: str, worker: str | None) -> str:
    """The database one process of run `run` uses; `worker` is xdist's `gwN`."""
    return f"{PREFIX}{run}_{worker}" if worker else f"{PREFIX}{run}"


def _run_pattern(run: str) -> str:
    """Postgres regex for every database of run `run`. Ids are [a-z0-9_] only."""
    return f"^{PREFIX}{run}(_gw[0-9]+)?$"


def pid_alive(pid: int) -> bool:
    """False only when no process with this pid exists on this host.

    Errs towards "alive": a pid now used by an unrelated process keeps a stale
    database for a while, which is harmless; the opposite would drop a live
    run's database.
    """
    if pid <= 0:
        return True  # 0 and negatives address process groups, not a process
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, belongs to another user
    except OverflowError:
        return False  # larger than any pid the kernel can hand out
    return True


def is_stale(
    name: str,
    connections: int,
    *,
    my_host: str,
    my_pid: int,
    alive: Callable[[int], bool] = pid_alive,
) -> bool:
    """True only when `name` belongs to a run that is provably dead.

    All of these must hold:

    - the name is a generated one (never an override, never an old-style name),
    - nobody is connected to it,
    - it was made on this host and pid namespace (a pid from anywhere else
      proves nothing here),
    - it is not this run's, and no process with that pid is alive.
    """
    match = _GENERATED.match(name)
    if match is None or connections:
        return False
    pid, host = int(match[1]), match[2]
    if host != my_host or pid == my_pid:
        return False
    return not alive(pid)


def run_sync[T](fn: Callable[..., Awaitable[T]], *args: object) -> T:
    """Run `fn(*args)` to completion on a private event loop in its own thread.

    For pytest's synchronous hooks and fixtures. `asyncio.run` on the main
    thread would reset the thread's current event loop to None when it
    finished, under pytest-asyncio's feet.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(fn(*args))).result()


async def connect_admin() -> asyncpg.Connection:
    return await asyncpg.connect(ADMIN_URL, timeout=5)


async def connections_to(conn: asyncpg.Connection, pattern: str) -> dict[str, int]:
    """Open connections per database whose name matches the Postgres regex."""
    rows = await conn.fetch(
        """
        SELECT d.datname, count(a.pid) AS n
        FROM pg_database d
        LEFT JOIN pg_stat_activity a ON a.datname = d.datname
        WHERE d.datname ~ $1
        GROUP BY d.datname
        """,
        pattern,
    )
    return {r["datname"]: r["n"] for r in rows}


async def databases_in_use(conn: asyncpg.Connection, run: str) -> dict[str, int]:
    """This run's databases that somebody is already connected to."""
    counts = await connections_to(conn, _run_pattern(run))
    return {name: n for name, n in counts.items() if n}


async def sweep_stale(
    conn: asyncpg.Connection, *, my_host: str, my_pid: int
) -> list[str]:
    """Drop test databases left behind by runs that are dead. Returns the names.

    The drop is a plain `DROP DATABASE`, never `WITH (FORCE)`: if a client
    connected after the count was taken, Postgres refuses and the database
    stays. That makes the no-connections rule hold at the moment of the drop,
    not just at the moment of the check.
    """
    dropped = []
    for name, n in sorted((await connections_to(conn, _GENERATED_SQL)).items()):
        if not is_stale(name, n, my_host=my_host, my_pid=my_pid):
            continue
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        except asyncpg.PostgresError:
            continue  # in use after all, or already gone: leave it
        dropped.append(name)
    return dropped


async def drop_run_databases(conn: asyncpg.Connection, run: str) -> list[str]:
    """Drop every database of run `run`. Returns the names.

    `WITH (FORCE)` is right here: these are this run's own, and a pool a test
    forgot to close must not keep one alive.
    """
    names = sorted(await connections_to(conn, _run_pattern(run)))
    for name in names:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    return names


async def settings_snapshot(conn: asyncpg.Connection) -> dict[str, str]:
    """Every `settings` row as key -> canonical jsonb text."""
    rows = await conn.fetch("SELECT key, value::text AS value FROM settings")
    return {r["key"]: r["value"] for r in rows}


async def restore_settings(conn: asyncpg.Connection, baseline: dict[str, str]) -> list[str]:
    """Put the `settings` table back to `baseline`. Returns the keys it touched.

    A row a test added is deleted, a row it changed or deleted is written back.
    Nothing is written when nothing differs, which is the common case. The
    value goes over the wire as text and is cast server-side, so no jsonb
    codec on `conn` can double-encode it.
    """
    current = await settings_snapshot(conn)
    added = sorted(k for k in current if k not in baseline)
    changed = sorted(k for k, v in baseline.items() if current.get(k) != v)
    if not added and not changed:
        return []
    async with conn.transaction():
        if added:
            await conn.execute("DELETE FROM settings WHERE key = ANY($1::text[])", added)
        for key in changed:
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES ($1, $2::text::jsonb) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                key,
                baseline[key],
            )
    return sorted(added + changed)
