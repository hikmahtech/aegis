"""The test suite's own database isolation (#325, #569).

#325: every pytest invocation gets its own databases, so two runs on one host
no longer drop each other's. #569: every test file starts from the seeded
`settings` table, so a row one file leaves behind cannot break the next file
on the same worker.

The end-to-end tests here start child pytest runs against the same Postgres
server, because the property under test is about separate invocations.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest

from tests import pg_test_db as testdb

_REPO = Path(__file__).resolve().parents[2]
_PROBES = "tests/core/db"
# Larger than any pid the kernel hands out (pid_max tops out at 4194304), so
# "no such process" is certain, and offset by our own pid so two concurrent
# runs of this file never pick the same name.
_DEAD_PID = 10_000_000 + os.getpid()


@pytest.fixture
def postgres(test_db_url) -> None:
    """Skip unless this run manages its own test databases on a live server."""
    if os.getenv("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL is caller-managed: no per-run databases")
    if test_db_url is None:
        pytest.skip("no Postgres reachable for the test database")


def _spawn(*args: str, env: dict[str, str] | None = None) -> subprocess.Popen:
    """Start a child pytest in this checkout, with none of our own run's state.

    Every PYTEST_* and AEGIS_TEST_* variable is dropped, so the child is a
    fresh invocation that makes its own run id — the xdist worker id in
    particular would otherwise make it name its database after ours.
    """
    child_env = {
        k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "AEGIS_TEST_"))
    }
    child_env.update(env or {})
    return subprocess.Popen(
        [
            sys.executable, "-m", "pytest", *args,
            "-p", "no:cacheprovider", "-o", "addopts=", "-q", "--tb=short", "--timeout=120",
        ],
        cwd=_REPO,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _finish(proc: subprocess.Popen) -> str:
    out, _ = proc.communicate(timeout=240)
    return out


async def _existing(names: list[str]) -> set[str]:
    conn = await testdb.connect_admin()
    try:
        rows = await conn.fetch(
            "SELECT datname FROM pg_database WHERE datname = ANY($1::text[])", names
        )
        return {r["datname"] for r in rows}
    finally:
        await conn.close()


# --- naming -----------------------------------------------------------------


def test_a_generated_run_id_is_this_pid_and_host(monkeypatch):
    monkeypatch.delenv(testdb.RUN_ID_ENV, raising=False)
    assert testdb.run_id() == f"{os.getpid()}_{testdb.host_tag()}"


def test_an_override_is_used_as_given(monkeypatch):
    monkeypatch.setenv(testdb.RUN_ID_ENV, "ci42")
    assert testdb.run_id() == "ci42"
    assert testdb.database_name("ci42", "gw3") == "aegis_test_ci42_gw3"
    assert testdb.database_name("ci42", None) == "aegis_test_ci42"


@pytest.mark.parametrize("bad", ["ci_42", "CI42", "ci-42", "x" * 25, "4194304_abcdef"])
def test_an_override_that_could_pass_for_a_generated_id_is_refused(monkeypatch, bad):
    """No underscore means an override never looks like `<pid>_<tag>` (which the
    sweep would judge) and never ends in `_gwN` (another run's worker)."""
    monkeypatch.setenv(testdb.RUN_ID_ENV, bad)
    with pytest.raises(ValueError, match=testdb.RUN_ID_ENV):
        testdb.run_id()


def test_every_name_fits_a_postgres_identifier():
    worst_generated = testdb.database_name("4194304_abcdef", "gw999")
    worst_override = testdb.database_name("x" * 24, "gw999")
    assert len(worst_generated.encode()) <= 63
    assert len(worst_override.encode()) <= 63


# --- the stale sweep --------------------------------------------------------


def test_pid_alive():
    assert testdb.pid_alive(os.getpid())
    assert not testdb.pid_alive(_DEAD_PID)
    assert not testdb.pid_alive(2**40)  # too big for the kernel: cannot exist
    assert testdb.pid_alive(0)  # a process group, not a process: never judged dead


@pytest.mark.parametrize(
    ("name", "connections", "stale"),
    [
        ("aegis_test_123_abcdef", 0, True),  # dead run of this host
        ("aegis_test_123_abcdef_gw4", 0, True),  # one of its workers
        ("aegis_test_123_abcdef", 1, False),  # somebody is connected
        ("aegis_test_123_000000", 0, False),  # another host: its pids mean nothing here
        ("aegis_test_555_abcdef_gw0", 0, False),  # this very run
        ("aegis_test_777_abcdef", 0, False),  # that run is alive
        ("aegis_test_gw0", 0, False),  # old-style name: a checkout still on the old code
        ("aegis_test", 0, False),
        ("aegis_test_ci42", 0, False),  # an override: never swept
        ("aegis_test_ci42_gw0", 0, False),
        ("aegis", 0, False),
    ],
)
def test_is_stale(name, connections, stale):
    alive = {555, 777}.__contains__
    assert testdb.is_stale(name, connections, my_host="abcdef", my_pid=555, alive=alive) is stale


def test_the_sweep_drops_only_databases_of_provably_dead_runs(postgres):
    """Real Postgres, under a made-up host tag so no other run's sweep can
    touch these databases and this sweep touches nobody else's."""
    real = testdb.host_tag()
    me = "c0ffee" if real != "c0ffee" else "c0ffef"
    other = "0ff0ff" if real != "0ff0ff" else "0ff0fe"
    dead = f"aegis_test_{_DEAD_PID}_{me}"
    dead_but_connected = f"aegis_test_{_DEAD_PID + 10_000_000}_{me}_gw1"
    alive = f"aegis_test_{os.getppid()}_{me}_gw0"
    own = f"aegis_test_{os.getpid()}_{me}"
    elsewhere = f"aegis_test_{_DEAD_PID + 20_000_000}_{other}"
    names = [dead, dead_but_connected, alive, own, elsewhere]

    async def scenario() -> tuple[list[str], set[str]]:
        admin = await testdb.connect_admin()
        try:
            for name in names:
                await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
                await admin.execute(f'CREATE DATABASE "{name}"')
            holder = await asyncpg.connect(f"{testdb.PG_SERVER}/{dead_but_connected}")
            try:
                dropped = await testdb.sweep_stale(admin, my_host=me, my_pid=os.getpid())
                left = await _existing(names)
            finally:
                await holder.close()
            return dropped, left
        finally:
            for name in names:  # all created by this test
                await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            await admin.close()

    dropped, left = testdb.run_sync(scenario)
    assert dead in dropped
    assert left == {dead_but_connected, alive, own, elsewhere}


# --- the settings baseline (#569) ------------------------------------------


async def test_restore_settings_undoes_a_change_an_addition_and_a_deletion(db_pool):
    async with db_pool.acquire() as conn:
        baseline = await testdb.settings_snapshot(conn)
        assert baseline["todoist_capture_enabled"] == "true"
        deleted = "user_timezone"
        assert deleted in baseline

        await conn.execute(
            "UPDATE settings SET value = 'false'::jsonb WHERE key = 'todoist_capture_enabled'"
        )
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('restore_probe', '{\"a\": 1}'::jsonb)"
        )
        await conn.execute("DELETE FROM settings WHERE key = $1", deleted)

        touched = await testdb.restore_settings(conn, baseline)
        assert touched == sorted(["todoist_capture_enabled", "restore_probe", deleted])
        assert await testdb.settings_snapshot(conn) == baseline
        assert await testdb.restore_settings(conn, baseline) == []  # nothing left to do


def test_the_migration_011_file_leaves_the_capture_switch_as_seeded(postgres, tmp_path):
    """#569 pinned. The migration file runs, then a probe file in the SAME
    process checks the kill switch — with the per-file reset switched off, so
    only the migration file's own clean-up can make it pass."""
    proc = _spawn(
        f"{_PROBES}/test_migration_011_todoist_capture.py",
        f"{_PROBES}/probe_isolation.py::test_capture_switch_is_on",
        env={
            "AEGIS_TEST_SETTINGS_RESET": "off",
            "AEGIS_PROBE_DIR": str(tmp_path),
            "AEGIS_PROBE_NAME": "run",
        },
    )
    out = _finish(proc)
    assert proc.returncode == 0, out
    # A run without xdist is named after its own pid, with no worker suffix,
    # and drops its database when it ends.
    name = (tmp_path / "run").read_text()
    assert name == f"aegis_test_{proc.pid}_{testdb.host_tag()}"
    assert testdb.run_sync(_existing, [name]) == set()


def test_the_per_file_reset_undoes_whatever_a_file_leaves_behind(postgres, tmp_path):
    """#569's general guard. One file changes, adds and deletes settings rows
    and restores none; the next file sees the seed. The same pair with the
    reset switched off must fail, or the check proves nothing."""
    files = (
        f"{_PROBES}/probe_settings_leak.py",
        f"{_PROBES}/probe_isolation.py::test_settings_are_as_seeded",
    )
    guarded = _spawn(*files)
    unguarded = _spawn(*files, env={"AEGIS_TEST_SETTINGS_RESET": "off"})
    guarded_out, unguarded_out = _finish(guarded), _finish(unguarded)
    assert guarded.returncode == 0, guarded_out
    assert unguarded.returncode == 1, unguarded_out
    assert "1 failed, 1 passed" in unguarded_out, unguarded_out


# --- concurrent runs (#325) -------------------------------------------------


def test_two_concurrent_runs_keep_their_own_databases(postgres, tmp_path):
    """#325 pinned. Two xdist runs start together; each holds its database
    until the other has created its own, then checks its data is intact.

    Under the old naming both were `aegis_test_gw0`, so the second run's
    DROP ... WITH (FORCE) took the first run's database from under it.
    """
    env = {"AEGIS_PROBE_DIR": str(tmp_path), "AEGIS_PROBE_PEERS": "a,b"}
    probe = f"{_PROBES}/probe_isolation.py::test_hold_database_until_peers_arrive"
    runs = {
        tag: _spawn(probe, "-n", "1", env={**env, "AEGIS_PROBE_NAME": tag}) for tag in ("a", "b")
    }
    outputs = {tag: _finish(proc) for tag, proc in runs.items()}
    for tag, proc in runs.items():
        assert proc.returncode == 0, f"run {tag}:\n{outputs[tag]}"

    # Each worker named its database after its CONTROLLER's pid, which is how
    # the run id reached it — a worker's own pid would differ.
    names = {tag: (tmp_path / tag).read_text() for tag in runs}
    for tag, proc in runs.items():
        assert names[tag] == f"aegis_test_{proc.pid}_{testdb.host_tag()}_gw0"
    assert names["a"] != names["b"]
    # And each run dropped its own database on the way out.
    assert testdb.run_sync(_existing, list(names.values())) == set()


def test_a_run_whose_database_is_in_use_stops_with_one_clear_message(postgres):
    """Two runs that share AEGIS_TEST_RUN_ID do collide — that is what a fixed
    name means. The second must say so and stop before any test, not drop the
    first run's database and bury the cause under per-test errors."""
    run = f"inuse{os.getpid()}"
    name = testdb.database_name(run, None)

    async def hold_and_run() -> tuple[int, str, set[str]]:
        admin = await testdb.connect_admin()
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            await admin.execute(f'CREATE DATABASE "{name}"')
            holder = await asyncpg.connect(f"{testdb.PG_SERVER}/{name}")
            try:
                proc = _spawn(
                    f"{_PROBES}/probe_isolation.py::test_capture_switch_is_on",
                    "-n", "1",
                    env={testdb.RUN_ID_ENV: run},
                )
                out = _finish(proc)
                left = await _existing([name])
            finally:
                await holder.close()
            return proc.returncode, out, left
        finally:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')  # ours
            await admin.close()

    returncode, out, left = testdb.run_sync(hold_and_run)
    assert returncode == 1, out
    assert "already in use" in out and name in out, out
    assert "passed" not in out and "failed" not in out, out  # stopped before any test
    assert left == {name}  # and did not drop it on the way out
