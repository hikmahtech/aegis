"""InfraHeartbeatFlow — transition matrix.

Covers: first-sight Down fires once; steady Down fires nothing; recovery
writes resolved row; stuck service needs 2 consecutive ticks; collect
failure threshold; dead-man only on success.
"""

from __future__ import annotations

import uuid

from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.infra_heartbeat import (
        InfraHeartbeatConfig,
        InfraHeartbeatFlow,
        _hb_fingerprint,
    )

_calls: dict = {}
_state: dict = {}


def _reset(collect: dict, prior: dict | None = None):
    _calls.clear()
    _calls.update({"spawned": [], "resolved": [], "written": [], "pinged": 0})
    _state.clear()
    _state["collect"] = collect
    _state["prior"] = prior or {"nodes": {}, "stuck": [], "confirmed": [], "fail_count": 0}


@activity.defn(name="collect_infra_state")
async def _collect() -> dict:
    return _state["collect"]


@activity.defn(name="read_heartbeat_state")
async def _read() -> dict:
    return _state["prior"]


@activity.defn(name="write_heartbeat_state")
async def _write(state: dict) -> None:
    _calls["written"].append(state)


@activity.defn(name="record_heartbeat_resolved")
async def _resolved(fingerprint: str) -> None:
    _calls["resolved"].append(fingerprint)


@activity.defn(name="ping_deadman")
async def _ping() -> dict:
    _calls["pinged"] += 1
    return {"pinged": True}


@activity.defn(name="get_heartbeat_routing")
async def _routing() -> dict:
    return {"infra_cluster": "homelab-swarm"}


@workflow.defn(name="AlertInvestigationFlow", sandboxed=False)
class _StubAlertFlow:
    @workflow.run
    async def run(self, alert: dict) -> dict:
        _calls["spawned"].append(alert)
        return {"status": "stub"}


_ACTS = [_collect, _read, _write, _resolved, _ping, _routing]


async def _run(config: InfraHeartbeatConfig | None = None) -> dict:
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=f"hb-{uuid.uuid4()}",
            workflows=[InfraHeartbeatFlow, _StubAlertFlow],
            activities=_ACTS,
        ) as worker,
    ):
        return await env.client.execute_workflow(
            InfraHeartbeatFlow.run,
            config or InfraHeartbeatConfig(),
            id=f"hb-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )


async def test_node_down_fires_once_with_escalate():
    _reset({"ok": True, "nodes": {"baa": "Ready", "noon": "Down"}, "stuck": [], "error": ""})
    result = await _run()
    assert result["alerts_spawned"] == 1
    alert = _calls["spawned"][0]
    assert alert["labels"]["alertname"] == "NodeDown"
    assert alert["fingerprint"] == _hb_fingerprint("NodeDown", "noon")
    assert alert["source"] == "aegis-heartbeat"
    assert alert["escalate"] is True
    assert alert["labels"]["cluster"] == "homelab-swarm"
    assert _calls["pinged"] == 1
    assert _calls["written"][0]["nodes"] == {"baa": "Ready", "noon": "Down"}


async def test_steady_down_fires_nothing():
    prior = {"nodes": {"baa": "Ready", "noon": "Down"}, "stuck": [], "confirmed": [], "fail_count": 0}
    _reset({"ok": True, "nodes": {"baa": "Ready", "noon": "Down"}, "stuck": [], "error": ""}, prior)
    result = await _run()
    assert result["alerts_spawned"] == 0
    assert _calls["resolved"] == []


async def test_recovery_writes_resolved_row_and_no_alert():
    prior = {"nodes": {"noon": "Down"}, "stuck": [], "confirmed": [], "fail_count": 0}
    _reset({"ok": True, "nodes": {"noon": "Ready"}, "stuck": [], "error": ""}, prior)
    result = await _run()
    assert result["alerts_spawned"] == 0
    assert _calls["resolved"] == [_hb_fingerprint("NodeDown", "noon")]


async def test_stuck_service_needs_two_consecutive_ticks():
    _reset({"ok": True, "nodes": {}, "stuck": ["koyracloud_order-finder"], "error": ""})
    await _run()
    assert _calls["spawned"] == []  # first sight — debounce
    prior = _calls["written"][0]
    assert prior["stuck"] == ["koyracloud_order-finder"]
    _reset({"ok": True, "nodes": {}, "stuck": ["koyracloud_order-finder"], "error": ""}, prior)
    await _run()
    assert len(_calls["spawned"]) == 1
    alert = _calls["spawned"][0]
    assert alert["labels"]["alertname"] == "DockerServiceDown"
    assert alert["labels"]["service_name"] == "koyracloud_order-finder"
    assert alert["escalate"] is False


async def test_confirmed_stuck_service_recovery_writes_resolved():
    prior = {"nodes": {}, "stuck": ["svc_a"], "confirmed": ["svc_a"], "fail_count": 0}
    _reset({"ok": True, "nodes": {}, "stuck": [], "error": ""}, prior)
    await _run()
    assert _calls["resolved"] == [_hb_fingerprint("DockerServiceDown", "svc_a")]


async def test_collect_failure_threshold_fires_once_and_no_ping():
    prior = {"nodes": {}, "stuck": [], "confirmed": [], "fail_count": 2}
    _reset({"ok": False, "nodes": {}, "stuck": [], "error": "ssh dead"}, prior)
    result = await _run(InfraHeartbeatConfig(fail_threshold=3))
    assert result["collect_ok"] is False
    assert len(_calls["spawned"]) == 1
    assert _calls["spawned"][0]["labels"]["alertname"] == "HeartbeatCollectFailed"
    assert _calls["pinged"] == 0
    assert _calls["written"][0]["fail_count"] == 3

    # 4th consecutive failure: no second alert. Capture the written state
    # into a local BEFORE calling _reset again, since _reset clears _calls.
    prior_after_third = _calls["written"][0]
    _reset({"ok": False, "nodes": {}, "stuck": [], "error": "ssh dead"}, prior_after_third)
    result2 = await _run(InfraHeartbeatConfig(fail_threshold=3))
    assert result2["collect_ok"] is False
    assert _calls["spawned"] == []
    assert _calls["pinged"] == 0
    assert _calls["written"][0]["fail_count"] == 4


async def test_collect_recovery_resolves_collect_alert():
    prior = {"nodes": {}, "stuck": [], "confirmed": [], "fail_count": 5}
    _reset({"ok": True, "nodes": {"baa": "Ready"}, "stuck": [], "error": ""}, prior)
    await _run()
    assert _hb_fingerprint("HeartbeatCollectFailed", "collect") in _calls["resolved"]
    assert _calls["written"][0]["fail_count"] == 0
