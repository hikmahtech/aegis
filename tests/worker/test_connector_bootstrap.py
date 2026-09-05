"""Connector construction failures must be observable, not silent (issue #205).

Before this, a connector whose constructor raised was logged at WARNING and
then simply left out of `deps.connectors`. Every activity that depended on it
saw `None`, took its "not configured" branch, and returned a tidy empty result
— i.e. the worker booted looking healthy and quietly stopped doing that job.

The split under test:
  * `knowledge` is FATAL — it is not an external integration, so it has no
    "operator hasn't set this up" state to degrade to.
  * everything else boots DEGRADED — ERROR log + `deps.connector_errors` +
    a stand-in that raises `ConnectorUnavailableError` at first use.

Shaped after B8's MCPManager (`core/src/aegis/mcp_manager.py`), which handles
a bad server entry the same way.
"""

from __future__ import annotations

from typing import Any

import pytest
from aegis.config import Settings
from aegis_worker import bootstrap as bootstrap_mod
from aegis_worker.activities.inventory import InventoryActivities
from aegis_worker.bootstrap import (
    ConnectorUnavailableError,
    _UnavailableConnector,
    bootstrap,
)
from structlog.testing import capture_logs
from temporalio.testing import ActivityEnvironment

PREFIX = "zzsf2-"

_BOOM = "ssh key file /nope/id_ed25519 is not readable"


@pytest.fixture
def worker_settings(test_settings, test_db_url) -> Settings:
    """Real `Settings` pointed at the session's test database.

    `bootstrap()` opens its own pool from `settings.database_url`, so the
    fixture's fake URL would make it fail before it ever reaches a connector.
    """
    if test_db_url is None:
        pytest.skip("no Postgres reachable for the test database")
    return test_settings.model_copy(update={"database_url": test_db_url})


async def _seed_repo_resource(pool) -> None:
    await pool.execute(
        "INSERT INTO resources (kind, slug, title, metadata) VALUES ('repository', $1, $2, $3)",
        f"{PREFIX}repo",
        f"{PREFIX}Repo",
        # The pool codec json.dumps() this — a pre-dumped string is rejected.
        {"github_repo": f"acme/{PREFIX}repo"},
    )


async def _wipe(pool) -> None:
    await pool.execute("DELETE FROM resources WHERE slug LIKE $1", f"{PREFIX}%")


# ---------------------------------------------------------------------------
# Boot-time signal
# ---------------------------------------------------------------------------


async def test_a_broken_connector_is_reported_and_the_worker_still_boots(
    worker_settings, monkeypatch
):
    """A degraded connector: ERROR at boot, recorded, worker up, rest healthy."""

    def explode(self, *args, **kwargs):
        raise OSError(_BOOM)

    monkeypatch.setattr(
        "aegis.connectors.remote_script.RemoteScriptConnector.__init__", explode
    )

    with capture_logs() as logs:
        deps = await bootstrap(worker_settings)
    try:
        # 1. Recorded, with the underlying reason — not merely absent.
        assert "remote_script" in deps.connector_errors
        assert _BOOM in deps.connector_errors["remote_script"]

        # 2. Logged at ERROR naming the connector. structlog bypasses stdlib
        #    logging, so caplog would see nothing here.
        failed = [e for e in logs if e["event"] == "connector_init_failed"]
        assert [e["connector"] for e in failed] == ["remote_script"]
        assert failed[0]["log_level"] == "error"
        assert failed[0]["fatal"] is False
        degraded = [e for e in logs if e["event"] == "worker_bootstrap_degraded"]
        assert degraded and degraded[0]["unavailable"] == ["remote_script"]
        assert degraded[0]["log_level"] == "error"

        # 3. The worker booted anyway, and the connectors that build fine are
        #    real objects — the failure did not take the healthy ones with it.
        assert deps.pool is not None
        assert isinstance(deps.connectors["remote_script"], _UnavailableConnector)
        assert not isinstance(deps.connectors["knowledge"], _UnavailableConnector)
        assert "knowledge" not in deps.connector_errors
    finally:
        await deps.close()  # must survive an _UnavailableConnector in the dict


async def test_a_fatal_connector_refuses_to_boot_and_names_itself(
    worker_settings, monkeypatch
):
    """`knowledge` is the one connector whose failure stops the worker."""
    pools: list[Any] = []
    real_create_pool = bootstrap_mod.create_pool

    async def tracking_create_pool(*args, **kwargs):
        pool = await real_create_pool(*args, **kwargs)
        pools.append(pool)
        return pool

    monkeypatch.setattr(bootstrap_mod, "create_pool", tracking_create_pool)

    def explode(self, *args, **kwargs):
        raise RuntimeError("embedding model missing")

    monkeypatch.setattr("aegis.services.knowledge.KnowledgeStore.__init__", explode)

    try:
        with capture_logs() as logs, pytest.raises(RuntimeError) as excinfo:
            await bootstrap(worker_settings)
        # The message must name the connector — "worker exited 1" with a
        # bare traceback is what made this class of failure hard to read.
        assert "knowledge" in str(excinfo.value)
        assert "embedding model missing" in str(excinfo.value)
        failed = [e for e in logs if e["event"] == "connector_init_failed"]
        assert failed[-1]["connector"] == "knowledge"
        assert failed[-1]["fatal"] is True
    finally:
        for pool in pools:
            await pool.close()


async def test_the_worker_builds_its_own_finance_connector(worker_settings):
    """Core builds a `FinanceConnector`; the worker has to build one too.

    `MoneyActivities.refresh_fx_prices` is the books' only source of USD/GBP/EUR
    rates. Without the connector here it reports itself `disabled` forever and
    every `-X ₹` figure in the money brief silently leaves foreign amounts
    unconverted — a wrong number, not a missing one.
    """
    from aegis.connectors.finance import FinanceConnector

    deps = await bootstrap(worker_settings)
    try:
        assert isinstance(deps.connectors.get("finance"), FinanceConnector)
        assert "finance" not in deps.connector_errors
    finally:
        await deps.close()


def test_the_money_activities_are_handed_the_finance_connector():
    """Registering a connector nobody passes on is the same as not having one,
    so the call site is pinned as well as the registration."""
    import ast
    import inspect

    import aegis_worker.__main__ as worker_main

    tree = ast.parse(inspect.getsource(worker_main))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "MoneyActivities"
    ]
    assert len(calls) == 1, "expected exactly one MoneyActivities construction in __main__"
    finance = [kw for kw in calls[0].keywords if kw.arg == "finance"]
    assert finance, "MoneyActivities must be given the finance connector"
    assert ast.unparse(finance[0].value) == "connectors.get('finance')"


# ---------------------------------------------------------------------------
# Use-time behaviour
# ---------------------------------------------------------------------------


async def test_dependent_activity_fails_with_the_reason_instead_of_a_clean_lie(
    db_pool,
):
    """The activity must stop, not return a reassuring empty result.

    `check_github_webhooks` is the sharp end of #205: handed `None` it answers
    "0 missing webhooks", which reads as a clean bill of health while nothing
    was actually checked.
    """
    await _wipe(db_pool)
    await _seed_repo_resource(db_pool)
    env = ActivityEnvironment()
    try:
        # Status quo for a connector that was never configured — still an
        # empty result, shown here so the contrast is not taken on trust. It
        # now at least labels itself `webhook_check_status='skipped'` (#142)
        # rather than being indistinguishable from a clean bill of health.
        silent = InventoryActivities(db_pool=db_pool, remote_script=None)
        assert await env.run(silent.check_github_webhooks) == {
            "missing_webhooks": [],
            "missing_webhooks_count": 0,
            "webhooks_newly_missing": [],
            "webhooks_recovered": [],
            "webhooks_inconclusive": [],
            "checked": 0,
            "skipped": 0,
            "webhook_check_status": "skipped",
        }

        # A connector that was configured and failed to build: same call, and
        # now it raises with the boot error attached rather than an
        # `AttributeError: 'NoneType' object has no attribute 'ensure_config'`
        # or a fabricated all-clear.
        broken = InventoryActivities(
            db_pool=db_pool,
            remote_script=_UnavailableConnector("remote_script", f"OSError: {_BOOM}"),
        )
        with pytest.raises(ConnectorUnavailableError) as excinfo:
            await env.run(broken.check_github_webhooks)
        assert "remote_script" in str(excinfo.value)
        assert _BOOM in str(excinfo.value)
    finally:
        await _wipe(db_pool)


async def test_the_stand_in_is_truthy_so_none_guards_do_not_swallow_it():
    """The `if not self.<connector>: skip` guards are everywhere.

    A falsy stand-in would sail straight through every one of them and restore
    the silent no-op this whole change exists to remove, so truthiness is
    load-bearing rather than incidental.
    """
    stand_in = _UnavailableConnector("homelab", "DockerException: no such context")
    assert bool(stand_in) is True
    assert "homelab" in repr(stand_in)
    # `close()` is a real no-op: WorkerDeps.close() probes with hasattr, which
    # only swallows AttributeError — a __getattr__ raising anything else would
    # turn shutdown into a crash.
    assert await stand_in.close() is None
    with pytest.raises(ConnectorUnavailableError):
        stand_in.list_services()
