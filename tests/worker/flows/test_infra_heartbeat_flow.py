"""InfraHeartbeatFlow — transition matrix.

Covers: first-sight Down fires once; steady Down fires nothing; recovery
writes resolved row; stuck service needs 2 consecutive ticks; collect
failure threshold; dead-man only on success; a node that vanishes from the
listing entirely (#131); the confirmed-stuck re-investigation (#138).
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
    _calls.update({"spawned": [], "resolved": [], "written": [], "pinged": 0, "quiet": []})
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


@activity.defn(name="ingest_alert")
async def _ingest(alert: dict, resolved: bool = False) -> dict:
    """The hub. A resolved transition lands in `_calls["resolved"]` by
    fingerprint (what the old resolved-row activity recorded); a firing one
    is always a new, investigable problem here."""
    if resolved:
        _calls["resolved"].append(alert["fingerprint"])
        return {"problem_id": "prob-r", "action": "resolved", "investigate": False}
    _calls.setdefault("ingested", []).append(alert["fingerprint"])
    return {
        "problem_id": f"prob-{alert['fingerprint']}",
        "action": "created",
        "investigate": True,
        "occurrences": 1,
        "todoist_task_id": None,
    }


@activity.defn(name="stale_stuck_problems")
async def _stale(subjects: list[str], hours: float) -> list[dict]:
    _calls.setdefault("stale_queries", []).append((list(subjects), hours))
    return [r for r in _state.get("stale", []) if r["subject"] in subjects]


@activity.defn(name="ping_deadman")
async def _ping() -> dict:
    _calls["pinged"] += 1
    return {"pinged": True}


@activity.defn(name="notify_node_transition")
async def _quiet_notify(node: str, status: str) -> None:
    _calls["quiet"].append((node, status))


@activity.defn(name="get_heartbeat_routing")
async def _routing() -> dict:
    return {"infra_cluster": "homelab-swarm"}


@activity.defn(name="clear_converged_deploys")
async def _clear_deploys(stuck: list[str]) -> dict:
    _calls.setdefault("clear_deploys", []).append(stuck)
    return {"cleared": ["aegis_core"] if not stuck else []}


@workflow.defn(name="AlertInvestigationFlow", sandboxed=False)
class _StubAlertFlow:
    @workflow.run
    async def run(self, alert: dict) -> dict:
        _calls["spawned"].append(alert)
        return {"status": "stub"}


_ACTS = [_collect, _read, _write, _ingest, _stale, _ping, _routing, _quiet_notify, _clear_deploys]


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


async def test_tick_asks_the_hub_to_clear_converged_deploys():
    prior = {"nodes": {"baa": "Ready"}, "stuck": ["x_svc"], "confirmed": [], "fail_count": 0}
    _reset({"ok": True, "nodes": {"baa": "Ready"}, "stuck": ["x_svc"], "error": ""}, prior)
    result = await _run()
    assert _calls["clear_deploys"] == [["x_svc"]]
    assert result["deploys_cleared"] == 0
    _reset({"ok": True, "nodes": {"baa": "Ready"}, "stuck": [], "error": ""})
    result = await _run()
    assert _calls["clear_deploys"] == [[]]
    assert result["deploys_cleared"] == 1


async def test_collect_failure_never_asks_the_hub():
    _reset({"ok": False, "nodes": {}, "stuck": [], "error": "ssh"})
    await _run()
    assert "clear_deploys" not in _calls


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


async def test_confirmed_stuck_service_recovery_resolves_the_problem():
    """One problem per service on the hub: DockerServiceDown and its PROLONGED
    re-investigations share it, so one resolve ends both."""
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


async def test_quiet_node_down_notifies_without_alert():
    """A quiet node (dual-boot box, expected to drop out) transitioning to Down
    sends a plain FYI notification and spawns NO investigation."""
    _reset({"ok": True, "nodes": {"baa": "Ready", "asif": "Down"}, "stuck": [], "error": ""})
    result = await _run(InfraHeartbeatConfig(quiet_nodes=["asif"]))
    assert result["alerts_spawned"] == 0
    assert result["quiet_notified"] == 1
    assert _calls["spawned"] == []
    assert _calls["quiet"] == [("asif", "down")]
    assert _calls["written"][0]["nodes"] == {"baa": "Ready", "asif": "Down"}


async def test_quiet_node_steady_down_stays_silent():
    prior = {"nodes": {"asif": "Down"}, "stuck": [], "confirmed": [], "fail_count": 0}
    _reset({"ok": True, "nodes": {"asif": "Down"}, "stuck": [], "error": ""}, prior)
    result = await _run(InfraHeartbeatConfig(quiet_nodes=["asif"]))
    assert result["quiet_notified"] == 0
    assert _calls["quiet"] == []
    assert _calls["spawned"] == []


async def test_quiet_node_recovery_notifies_and_still_writes_resolved():
    """Recovery of a quiet node: FYI ping plus the resolved audit row (harmless,
    and it closes out any alert fired before the node was quieted)."""
    prior = {"nodes": {"asif": "Down"}, "stuck": [], "confirmed": [], "fail_count": 0}
    _reset({"ok": True, "nodes": {"asif": "Ready"}, "stuck": [], "error": ""}, prior)
    result = await _run(InfraHeartbeatConfig(quiet_nodes=["asif"]))
    assert result["quiet_notified"] == 1
    assert _calls["quiet"] == [("asif", "up")]
    assert _calls["resolved"] == [_hb_fingerprint("NodeDown", "asif")]
    assert _calls["spawned"] == []


async def test_non_quiet_node_still_alerts_when_quiet_list_set():
    _reset({"ok": True, "nodes": {"asif": "Ready", "noon": "Down"}, "stuck": [], "error": ""})
    result = await _run(InfraHeartbeatConfig(quiet_nodes=["asif"]))
    assert result["alerts_spawned"] == 1
    assert _calls["spawned"][0]["fingerprint"] == _hb_fingerprint("NodeDown", "noon")
    assert _calls["quiet"] == []


# --------------------------------------------------------------------------
# #131 — a node that disappears from `docker node ls` entirely
# --------------------------------------------------------------------------


async def test_node_vanished_while_down_resolves_its_alert():
    """The bug: the diff walked cur_nodes only, so a Down node that dropped out
    of the listing never got its resolved row — an escalating NodeDown that can
    then only stop at ack/max-repeats/48h archive."""
    prior = {
        "nodes": {"baa": "Ready", "noon": "Down"},
        "stuck": [],
        "confirmed": [],
        "fail_count": 0,
    }
    _reset({"ok": True, "nodes": {"baa": "Ready"}, "stuck": [], "error": ""}, prior)
    result = await _run()
    assert result["nodes_vanished"] == 1
    assert _calls["resolved"] == [_hb_fingerprint("NodeDown", "noon")]
    assert result["alerts_spawned"] == 0
    assert _calls["written"][0]["nodes"] == {"baa": "Ready"}


async def test_node_vanished_while_ready_resolves_nothing():
    """Only a node that was Down owns an open alert. A Ready node leaving the
    swarm must not write a resolved row for an alert that never fired."""
    prior = {
        "nodes": {"baa": "Ready", "lam": "Ready"},
        "stuck": [],
        "confirmed": [],
        "fail_count": 0,
    }
    _reset({"ok": True, "nodes": {"baa": "Ready"}, "stuck": [], "error": ""}, prior)
    result = await _run()
    assert result["nodes_vanished"] == 0
    assert _calls["resolved"] == []


async def test_empty_node_listing_does_not_resolve_every_down_node():
    """An ok-but-empty listing is a collection anomaly, not a mass
    decommission: resolving on it would close real escalations."""
    prior = {
        "nodes": {"baa": "Ready", "noon": "Down"},
        "stuck": [],
        "confirmed": [],
        "fail_count": 0,
    }
    _reset({"ok": True, "nodes": {}, "stuck": [], "error": ""}, prior)
    result = await _run()
    assert result["nodes_vanished"] == 0
    assert _calls["resolved"] == []


async def test_empty_node_listing_keeps_the_last_good_node_map():
    """...and it must not erase the stored Down status either — that erasure is
    the other half of the #131 orphan."""
    prior = {
        "nodes": {"baa": "Ready", "noon": "Down"},
        "stuck": [],
        "confirmed": [],
        "fail_count": 0,
    }
    _reset({"ok": True, "nodes": {}, "stuck": [], "error": ""}, prior)
    await _run()
    assert _calls["written"][0]["nodes"] == {"baa": "Ready", "noon": "Down"}


# --------------------------------------------------------------------------
# #138 — a confirmed-stuck service is never retried
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# #138 — a confirmed-stuck service is re-investigated on the hub's say-so
# --------------------------------------------------------------------------


async def test_stale_stuck_service_is_reinvestigated_on_its_own_problem():
    svc = "miniflux_miniflux"
    collect = {"ok": True, "nodes": {"baa": "Ready"}, "stuck": [svc], "error": ""}
    prior = {"nodes": {}, "stuck": [svc], "confirmed": [svc], "fail_count": 0}
    _reset(collect, prior)
    _state["stale"] = [{"id": "prob-old", "subject": svc, "hours": 30.0}]
    result = await _run()
    assert result["services_reinvestigated"] == 1
    assert _calls["stale_queries"] == [([svc], 24.0)]
    assert len(_calls["spawned"]) == 1
    alert = _calls["spawned"][0]
    assert alert["labels"]["alertname"] == "ServiceDownProlonged"
    assert alert["labels"]["service_name"] == svc
    assert alert["problem_id"] == "prob-old"
    assert alert["escalate"] is True
    # a re-investigation is not a new occurrence: nothing was ingested for it
    assert _calls.get("ingested", []) == []
    # and the state row carries no clocks any more
    assert "confirmed_at" not in _calls["written"][0]


async def test_hub_says_nothing_is_stale_so_nothing_is_reinvestigated():
    svc = "koyra-drwhome_drwhome-jobs"
    collect = {"ok": True, "nodes": {"baa": "Ready"}, "stuck": [svc], "error": ""}
    prior = {"nodes": {}, "stuck": [svc], "confirmed": [svc], "fail_count": 0}
    _reset(collect, prior)
    result = await _run()
    assert result["services_reinvestigated"] == 0
    assert _calls["stale_queries"] == [([svc], 24.0)]
    assert _calls["spawned"] == []


async def test_restuck_hours_zero_never_asks_the_hub():
    svc = "koyracloud_redis"
    collect = {"ok": True, "nodes": {"baa": "Ready"}, "stuck": [svc], "error": ""}
    prior = {"nodes": {}, "stuck": [svc], "confirmed": [svc], "fail_count": 0}
    _reset(collect, prior)
    _state["stale"] = [{"id": "prob-old", "subject": svc, "hours": 240.0}]
    result = await _run(InfraHeartbeatConfig(restuck_hours=0))
    assert result["services_reinvestigated"] == 0
    assert "stale_queries" not in _calls
    assert _calls["spawned"] == []


async def test_firing_transition_the_hub_declines_spawns_nothing():
    """A repeat the hub attaches (investigate=False) starts no child."""
    _reset({"ok": True, "nodes": {"baa": "Ready", "noon": "Down"}, "stuck": [], "error": ""})

    @activity.defn(name="ingest_alert")
    async def _decline(alert: dict, resolved: bool = False) -> dict:
        return {"problem_id": "prob-x", "action": "attached", "investigate": False}

    acts = [a for a in _ACTS if a is not _ingest] + [_decline]
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=f"hb-{uuid.uuid4()}",
            workflows=[InfraHeartbeatFlow, _StubAlertFlow],
            activities=acts,
        ) as worker,
    ):
        result = await env.client.execute_workflow(
            InfraHeartbeatFlow.run,
            InfraHeartbeatConfig(),
            id=f"hb-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    assert result["alerts_spawned"] == 0
    assert _calls["spawned"] == []
