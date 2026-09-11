"""#501 end to end: the flow, the real hub and the real restart activity.

The koyra services on 2026-09-11 went: down, restarted, recovered, down again
ten minutes later, restarted again. This replays that against the test
database: the same service breaks twice, each time through the hub's own
ingest (the second is a reopen of the same problem), with only the swarm, the
investigation and chat stubbed.
"""

from __future__ import annotations

import uuid

import pytest
from aegis.services import hub_project
from aegis.services.hub import list_events
from aegis_worker.activities import alerts as alerts_mod
from aegis_worker.activities.alerts import AlertActivities
from aegis_worker.activities.hub import HubActivities
from aegis_worker.flows.alert_investigation import AlertInvestigationFlow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.worker.flows import _alert_flow_harness as h
from tests.worker.test_alert_restart_once import FakeHomelab, task

_SCHEDULING = "no suitable node (insufficient resources on 3 nodes)"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(alerts_mod, "_REMEDIATE_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(alerts_mod, "_REMEDIATE_POLLS", 2)

    async def _no_note(pool, settings, task_id, text):
        return True

    # The projector's own comments; the flow's go through the stub.
    monkeypatch.setattr(hub_project, "_post_note", _no_note)


def _alert(service: str) -> dict:
    """What the heartbeat sends, without a problem id, so the flow's step 0
    puts each one through the hub exactly as a producer would."""
    return {
        "title": f"Service {service} down",
        "fingerprint": f"aegis-heartbeat:DockerServiceDown:{service}",
        "severity": "critical",
        "source": "aegis-heartbeat",
        "service": service,
        "description": f"{service} below desired replicas",
        "labels": {"alertname": "DockerServiceDown", "service_name": service},
        "escalate": False,
        "todoist_task_id": f"T-{uuid.uuid4().hex[:8]}",
    }


def _activities(pool, homelab) -> list:
    hub = HubActivities(db_pool=pool)
    alerts = AlertActivities(db_pool=pool, homelab_connector=homelab)
    real = [
        hub.ingest_alert,
        hub.problem_status,
        hub.record_investigation,
        hub.verification_delay,
        hub.mute_problem,
        alerts.get_alert_routing_config,
        alerts.remediate_infra_service,
        alerts.recent_auto_restart,
    ]
    replaced = {
        "ingest_alert",
        "problem_status",
        "record_investigation",
        "verification_delay",
        "mute_problem",
        "get_alert_routing_config",
        "remediate_infra_service",
        "recent_auto_restart",
    }
    stubs = [s for s in h.STUBS if s.__temporal_activity_definition.name not in replaced]
    return real + stubs


async def _run_twice(pool, homelab, service: str, *, between=None) -> tuple[dict, dict]:
    alert = _alert(service)
    tq = h.task_queue()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=tq,
            workflows=[AlertInvestigationFlow, h.FakeInteractionFlow],
            activities=_activities(pool, homelab),
        ),
    ):
        first = await env.client.execute_workflow(
            AlertInvestigationFlow.run, alert, id=f"restart-1-{uuid.uuid4().hex[:8]}", task_queue=tq
        )
        if between is not None:
            await between(first)
        second = await env.client.execute_workflow(
            AlertInvestigationFlow.run, alert, id=f"restart-2-{uuid.uuid4().hex[:8]}", task_queue=tq
        )
    return first, second


def _svc() -> str:
    return f"zzrestart-{uuid.uuid4().hex[:8]}_web"


async def test_a_service_that_breaks_again_inside_the_window_gets_one_restart_then_a_card(db_pool):
    svc = _svc()
    homelab = FakeHomelab(
        [task(svc, "t2", "Running 5 seconds ago"), task(svc, "t1", "Rejected 1 minute ago", _SCHEDULING)]
    )
    h.reset()
    h.S.verdict = {**h.S.verdict, "status": "inconclusive"}

    first, second = await _run_twice(db_pool, homelab, svc)

    assert first["status"] == "auto_remediated"
    assert second["problem_id"] == first["problem_id"], "the return is the same problem"
    assert homelab.restarted == [svc], "restarted once, not twice"
    assert second["restart_repeat"] is True and second["decision_card"] is True
    assert len(h.S.cards) == 1
    assert _SCHEDULING in h.S.cards[0].prompt

    events = await list_events(db_pool, first["problem_id"], limit=100)
    by_step = {
        e["external_id"].rsplit(":", 1)[-1]: e["payload"]
        for e in events
        if e["kind"] == "investigation"
    }
    assert by_step["auto_remediated"]["remediation"]["diagnostics"][1]["error"] == _SCHEDULING
    assert by_step["restart_repeat"]["restart_repeat"]["diagnostics_then"][1]["error"] == _SCHEDULING


async def test_a_service_that_breaks_again_after_the_window_is_restarted_again(db_pool):
    svc = _svc()
    homelab = FakeHomelab([task(svc, "t1", "Running 5 seconds ago")])
    h.reset()

    async def _an_hour_passes(first: dict) -> None:
        await db_pool.execute(
            "UPDATE problem_events SET occurred_at = now() - interval '61 minutes' "
            "WHERE problem_id = $1::uuid AND kind = 'investigation'",
            first["problem_id"],
        )

    first, second = await _run_twice(db_pool, homelab, svc, between=_an_hour_passes)

    assert first["status"] == second["status"] == "auto_remediated"
    assert homelab.restarted == [svc, svc]
    assert h.S.cards == []
