"""InfraHeartbeatFlow's cluster-outage detector (#630).

`outage_min_nodes` or more swarm nodes not ready at once is one `ClusterOutage`
alert with `aegis_class: outage`, raised on the transition and before the
tick's own node and service alerts, so those already land inside the window
the hub opens for it. Fewer again resolves it.
"""

from __future__ import annotations

import uuid

from temporalio import workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from tests.worker.flows import test_infra_heartbeat_flow as hb

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.infra_heartbeat import (
        PATCH_CLUSTER_OUTAGE,
        InfraHeartbeatConfig,
        InfraHeartbeatFlow,
        _hb_fingerprint,
    )

_OUTAGE_FP = _hb_fingerprint("ClusterOutage", "")


def _prior(nodes: dict, outage: bool = False) -> dict:
    return {"nodes": nodes, "stuck": [], "confirmed": [], "fail_count": 0, "outage": outage}


async def test_one_node_down_is_not_an_outage():
    hb._reset({"ok": True, "nodes": {"baa": "Ready", "noon": "Down", "wow": "Ready"}, "stuck": [], "error": ""})
    result = await hb._run()
    assert _OUTAGE_FP not in hb._calls.get("ingested", [])
    assert result["outage"] is False and result["nodes_not_ready"] == 1
    assert hb._calls["written"][-1]["outage"] is False


async def test_two_nodes_not_ready_raise_one_outage_before_the_node_alerts():
    hb._reset(
        {
            "ok": True,
            "nodes": {"baa": "Ready", "noon": "Down", "wow": "Unknown", "meem": "Down"},
            "stuck": [],
            "error": "",
        }
    )
    result = await hb._run()

    ingested = hb._calls["ingested"]
    # First, so the NodeDown alerts of this same tick meet the window.
    assert ingested[0] == _OUTAGE_FP
    assert ingested.count(_OUTAGE_FP) == 1
    [outage] = [a for a in hb._calls["spawned"] if a["fingerprint"] == _OUTAGE_FP]
    assert outage["labels"]["alertname"] == "ClusterOutage"
    assert outage["labels"]["aegis_class"] == "outage"
    assert "service_name" not in outage["labels"]
    assert "meem, noon, wow" in outage["description"]
    assert result["outage"] is True and result["nodes_not_ready"] == 3
    assert hb._calls["written"][-1]["outage"] is True


async def test_a_steady_outage_raises_nothing_more():
    nodes = {"baa": "Ready", "noon": "Down", "wow": "Down"}
    hb._reset({"ok": True, "nodes": nodes, "stuck": [], "error": ""}, _prior(nodes, outage=True))
    result = await hb._run()
    assert _OUTAGE_FP not in hb._calls.get("ingested", [])
    assert hb._calls["resolved"] == []
    assert result["outage"] is True


async def test_fewer_than_the_threshold_again_resolves_it():
    prior = _prior({"baa": "Ready", "noon": "Down", "wow": "Down"}, outage=True)
    hb._reset({"ok": True, "nodes": {"baa": "Ready", "noon": "Down", "wow": "Ready"}, "stuck": [], "error": ""}, prior)
    result = await hb._run()
    assert _OUTAGE_FP in hb._calls["resolved"]
    assert result["outage"] is False
    assert hb._calls["written"][-1]["outage"] is False


async def test_the_threshold_comes_from_the_config_and_quiet_nodes_do_not_count():
    nodes = {"baa": "Ready", "noon": "Down", "wow": "Down", "pop": "Down"}
    # Three down, but `pop` is a dual-boot box that leaves on purpose.
    hb._reset({"ok": True, "nodes": nodes, "stuck": [], "error": ""})
    result = await hb._run(InfraHeartbeatConfig(outage_min_nodes=3, quiet_nodes=["pop"]))
    assert result["outage"] is False and result["nodes_not_ready"] == 2
    hb._reset({"ok": True, "nodes": nodes, "stuck": [], "error": ""})
    result = await hb._run(InfraHeartbeatConfig(outage_min_nodes=3))
    assert result["outage"] is True
    # 0 turns the detector off.
    hb._reset({"ok": True, "nodes": nodes, "stuck": [], "error": ""})
    result = await hb._run(InfraHeartbeatConfig(outage_min_nodes=0))
    assert result["outage"] is False
    assert _OUTAGE_FP not in hb._calls.get("ingested", [])


async def test_an_empty_listing_does_not_end_an_outage():
    """An ok-but-empty node listing is a collection anomaly, not every node
    recovering at once."""
    prior = _prior({"noon": "Down", "wow": "Down"}, outage=True)
    hb._reset({"ok": True, "nodes": {}, "stuck": [], "error": ""}, prior)
    result = await hb._run()
    assert _OUTAGE_FP not in hb._calls["resolved"]
    assert result["outage"] is True


def test_outage_min_nodes_is_read_from_activities_config():
    from aegis_worker.registry import FLOWS

    spec = next(s for s in FLOWS if s.flow is InfraHeartbeatFlow)
    row = {"agent_id": "pandoras-actor", "_settings": {}}
    assert spec.schedule_config({**row, "config": {}}).outage_min_nodes == 2
    assert spec.schedule_config({**row, "config": {"outage_min_nodes": 4}}).outage_min_nodes == 4


async def test_a_tick_from_before_the_detector_replays(monkeypatch):
    """A heartbeat in flight across the deploy recorded no outage step; the
    new flow must replay it (the step is behind `workflow.patched`)."""
    real = workflow.patched
    hb._reset({"ok": True, "nodes": {"baa": "Ready", "noon": "Down", "wow": "Down"}, "stuck": [], "error": ""})
    monkeypatch.setattr(
        workflow, "patched", lambda pid: False if pid == PATCH_CLUSTER_OUTAGE else real(pid)
    )
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=f"hb-{uuid.uuid4()}",
            workflows=[InfraHeartbeatFlow, hb._StubAlertFlow],
            activities=hb._ACTS,
        ) as worker,
    ):
        handle = await env.client.start_workflow(
            InfraHeartbeatFlow.run,
            InfraHeartbeatConfig(),
            id=f"hb-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
        history = await handle.fetch_history()
    monkeypatch.undo()
    # The premise: this tick really ran the code from before the detector.
    assert _OUTAGE_FP not in hb._calls.get("ingested", [])
    await Replayer(workflows=[InfraHeartbeatFlow]).replay_workflow(history)
