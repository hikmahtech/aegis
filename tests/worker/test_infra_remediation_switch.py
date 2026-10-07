"""`alert_remediation.enabled` (aegis#705): the lever that turns v1's infra writes off for the
a2-devops cutover. Off: no automatic restart, and a Gate-2 run refuses every command that
changes something; read-only checks still run."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis.services import alert_remediation as core
from aegis_worker.activities.alerts import AlertActivities, infra_remediation_enabled
from temporalio.testing import ActivityEnvironment


def test_enabled_is_read_leniently():
    assert core.enabled(None) is True
    assert core.enabled({"repeat_window_minutes": 60}) is True
    assert core.enabled({"enabled": "no"}) is True  # only a stored false is off
    assert core.enabled({"enabled": False}) is False


def test_validate_takes_enabled_as_a_bool():
    assert core.validate({"repeat_window_minutes": 60, "enabled": False}) == {
        "repeat_window_minutes": 60, "enabled": False}
    with pytest.raises(ValueError, match="true or false"):
        core.validate({"repeat_window_minutes": 60, "enabled": "off"})
    assert core.merge({"repeat_window_minutes": 15, "enabled": False}) == {
        "repeat_window_minutes": 15}


@pytest_asyncio.fixture(loop_scope="function")
async def clean(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key = 'alert_remediation'")
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = 'alert_remediation'")


async def test_the_worker_reads_the_switch(clean):
    assert await infra_remediation_enabled(clean) is True
    await core.save_alert_remediation(clean, {"repeat_window_minutes": 60, "enabled": False})
    assert await infra_remediation_enabled(clean) is False
    assert await infra_remediation_enabled(None) is True


async def test_switched_off_the_restart_does_nothing(clean):
    await core.save_alert_remediation(clean, {"repeat_window_minutes": 60, "enabled": False})
    homelab = AsyncMock()
    act = AlertActivities(db_pool=clean, homelab_connector=homelab)
    alert = {"labels": {"alertname": "DockerServiceDown", "service_name": "web_web"}}
    out = await ActivityEnvironment().run(act.remediate_infra_service, alert)
    assert out["attempted"] is False and out["reason"] == "disabled"
    homelab.restart_service.assert_not_awaited()


async def test_switched_off_a_gate2_run_refuses_changes_but_runs_checks(clean):
    await core.save_alert_remediation(clean, {"repeat_window_minutes": 60, "enabled": False})
    remote = AsyncMock()
    remote.run_on_host.return_value = {"status": "ok", "exit_code": 0, "stdout": "", "stderr": ""}
    act = AlertActivities(db_pool=clean, remote_script=remote)
    out = await ActivityEnvironment().run(
        act.run_remediation_commands, ["docker node ls", "docker service update --force a"],
        "meem", "fix")
    assert out["refused"] == "remediation_disabled" and out["ran"] == []
    remote.run_on_host.assert_not_awaited()
