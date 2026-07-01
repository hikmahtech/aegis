"""Tests for infra auto-remediation: AlertActivities.remediate_infra_service
and the Gate-0 repo-confirm card candidate menu.

The activity force-restarts a swarm service that fell below desired replicas
(DockerServiceDown / ServiceDownProlonged) and polls it back to healthy. A
crash-loop (ServiceCrashLooping) must NOT be auto-restarted — restarting it
just churns; that case stays on the investigation path.
"""

from unittest.mock import AsyncMock

import pytest
from aegis_worker.activities import alerts as alerts_mod
from aegis_worker.activities.alerts import AlertActivities
from aegis_worker.flows.alert_investigation import _build_repo_confirm_prompt
from temporalio.testing import ActivityEnvironment


def _alert(alertname: str, **labels) -> dict:
    return {"title": "svc down", "source": "alertmanager", "labels": {"alertname": alertname, **labels}}


def _fake_homelab(restart_ok=True, services=None):
    hl = AsyncMock()
    hl.restart_service.return_value = {"ok": restart_ok, "data": {"output": "updated"}, "error": None if restart_ok else "boom"}
    hl.list_services.return_value = {"ok": True, "data": services or [], "error": None}
    return hl


@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch):
    # Don't sleep 5s/poll in tests.
    monkeypatch.setattr(alerts_mod, "_REMEDIATE_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(alerts_mod, "_REMEDIATE_POLLS", 3)


@pytest.mark.asyncio
async def test_crash_loop_is_not_remediated():
    # The whole point: a crash-loop must not be force-restarted.
    act = AlertActivities(homelab_connector=_fake_homelab())
    res = await ActivityEnvironment().run(
        act.remediate_infra_service, _alert("ServiceCrashLooping", service="x")
    )
    assert res["attempted"] is False
    assert res["reason"].startswith("not_remediable_class")
    act.homelab_connector.restart_service.assert_not_called()


@pytest.mark.asyncio
async def test_non_infra_class_skipped():
    act = AlertActivities(homelab_connector=_fake_homelab())
    res = await ActivityEnvironment().run(
        act.remediate_infra_service, _alert("NodeDown", hostname="node-a")
    )
    assert res["attempted"] is False


@pytest.mark.asyncio
async def test_no_service_name_skipped():
    act = AlertActivities(homelab_connector=_fake_homelab())
    res = await ActivityEnvironment().run(act.remediate_infra_service, _alert("DockerServiceDown"))
    assert res["attempted"] is False
    assert res["reason"] == "no_service_name"


@pytest.mark.asyncio
async def test_no_connector_skipped():
    act = AlertActivities(homelab_connector=None)
    res = await ActivityEnvironment().run(
        act.remediate_infra_service, _alert("DockerServiceDown", service_name="trading_worker")
    )
    assert res["attempted"] is False
    assert res["reason"] == "no_homelab_connector"


@pytest.mark.asyncio
async def test_recovers_after_restart():
    hl = _fake_homelab(services=[{"name": "trading_worker", "replicas_actual": 2, "replicas_desired": 2}])
    act = AlertActivities(homelab_connector=hl)
    res = await ActivityEnvironment().run(
        act.remediate_infra_service, _alert("DockerServiceDown", service_name="trading_worker")
    )
    assert res["attempted"] is True
    assert res["recovered"] is True
    assert res["service"] == "trading_worker"
    hl.restart_service.assert_awaited_once_with("trading_worker")


@pytest.mark.asyncio
async def test_restart_command_failure():
    hl = _fake_homelab(restart_ok=False)
    act = AlertActivities(homelab_connector=hl)
    res = await ActivityEnvironment().run(
        act.remediate_infra_service, _alert("DockerServiceDown", service_name="trading_worker")
    )
    assert res["attempted"] is True
    assert res["recovered"] is False
    assert res["reason"].startswith("restart_failed")
    hl.list_services.assert_not_called()


@pytest.mark.asyncio
async def test_restart_issued_but_not_converged():
    # Service stays below desired across all polls → not recovered.
    hl = _fake_homelab(services=[{"name": "trading_worker", "replicas_actual": 0, "replicas_desired": 2}])
    act = AlertActivities(homelab_connector=hl)
    res = await ActivityEnvironment().run(
        act.remediate_infra_service, _alert("DockerServiceDown", service_name="trading_worker")
    )
    assert res["attempted"] is True
    assert res["recovered"] is False
    assert res["reason"] == "restart_issued_not_converged"


@pytest.mark.asyncio
async def test_service_label_fallback():
    # ServiceDownProlonged carries `service_name`; alert-level `service` is the
    # last fallback when neither label is present.
    hl = _fake_homelab(services=[{"name": "api", "replicas_actual": 1, "replicas_desired": 1}])
    act = AlertActivities(homelab_connector=hl)
    alert = {"title": "x", "source": "alertmanager", "service": "api", "labels": {"alertname": "ServiceDownProlonged"}}
    res = await ActivityEnvironment().run(act.remediate_infra_service, alert)
    assert res["recovered"] is True
    hl.restart_service.assert_awaited_once_with("api")


def test_repo_confirm_prompt_lists_candidates():
    candidates = [
        {"resource_title": "Acme BCP — data", "resource_path": "acme/bcp",
         "github_repo": "Acme/bcp", "label": "Acme/bcp", "score": 1.0},
        {"resource_title": "Screener P-Server", "resource_path": "acme/screener-p-server",
         "github_repo": "Acme/screener-p-server", "label": "Acme/screener-p-server", "score": 0.5},
    ]
    out = _build_repo_confirm_prompt(
        title="PublisherException", source="sentry", severity="error",
        service="bcp", description="export failed", task_id="6gw1", candidates=candidates,
    )
    # Numbered menu, friendly titles, repo identity, and match strength all present.
    assert "<b>1.</b> Acme BCP — data" in out
    assert "<code>Acme/bcp</code>" in out
    assert "strong match" in out
    assert "<b>2.</b> Screener P-Server" in out
    assert "possible match" in out


def test_repo_confirm_prompt_no_candidates_is_safe():
    out = _build_repo_confirm_prompt(
        title="x", source="s", severity="warn", service="", description="", task_id="", candidates=None,
    )
    assert "Which repository" in out
