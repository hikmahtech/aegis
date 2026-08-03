"""InfraHeartbeatFlow — transition matrix.

Covers: first-sight Down fires once; steady Down fires nothing; recovery
writes resolved row; stuck service needs 2 consecutive ticks; collect
failure threshold; dead-man only on success; a node that vanishes from the
listing entirely (#131); the confirmed-stuck re-investigation (#138).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

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


@activity.defn(name="record_heartbeat_resolved")
async def _resolved(fingerprint: str) -> None:
    _calls["resolved"].append(fingerprint)


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


@workflow.defn(name="AlertInvestigationFlow", sandboxed=False)
class _StubAlertFlow:
    @workflow.run
    async def run(self, alert: dict) -> dict:
        _calls["spawned"].append(alert)
        return {"status": "stub"}


_ACTS = [_collect, _read, _write, _resolved, _ping, _routing, _quiet_notify]


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
    """Both fingerprints: a service that was re-investigated (#138) owns a live
    ServiceDownProlonged escalation as well, and it needs its own resolved row
    or it keeps nagging after the outage is over."""
    prior = {"nodes": {}, "stuck": ["svc_a"], "confirmed": ["svc_a"], "fail_count": 0}
    _reset({"ok": True, "nodes": {}, "stuck": [], "error": ""}, prior)
    await _run()
    assert _calls["resolved"] == [
        _hb_fingerprint("DockerServiceDown", "svc_a"),
        _hb_fingerprint("ServiceDownProlonged", "svc_a"),
    ]


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


def _backdate(state: dict, key: str, svc: str, hours: float) -> dict:
    """Rewind one per-service clock in a state dict the flow itself wrote.

    Reads back the stamp `workflow.now()` produced instead of inventing one
    from pytest's clock, so these tests don't depend on the time-skipping test
    server sharing a wall clock with the test process.
    """
    out = {**state, key: dict(state.get(key) or {})}
    out[key][svc] = (
        datetime.fromisoformat(out[key][svc]) - timedelta(hours=hours)
    ).isoformat()
    return out


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


async def test_confirmed_stuck_service_reinvestigates_once_not_every_tick():
    """miniflux_miniflux sat `confirmed` for >24h with no retry and nobody told.

    Asserts the COUNT across three consecutive polls: the re-investigation must
    fire exactly once, not once every 2 minutes.
    """
    svc = "miniflux_miniflux"
    collect = {"ok": True, "nodes": {"baa": "Ready"}, "stuck": [svc], "error": ""}

    # Tick 1 — already confirmed when this code shipped: clock starts now.
    prior = {"nodes": {}, "stuck": [svc], "confirmed": [svc], "fail_count": 0}
    _reset(collect, prior)
    first = await _run()
    assert first["services_reinvestigated"] == 0
    assert _calls["spawned"] == []
    seeded = _calls["written"][0]
    assert svc in seeded["confirmed_at"]

    # Tick 2 — same service, 30h of being stuck later.
    aged = _backdate(seeded, "confirmed_at", svc, 30)
    _reset(collect, aged)
    second = await _run()
    assert second["services_reinvestigated"] == 1
    assert len(_calls["spawned"]) == 1
    alert = _calls["spawned"][0]
    assert alert["labels"]["alertname"] == "ServiceDownProlonged"
    assert alert["labels"]["service_name"] == svc
    assert alert["fingerprint"] == _hb_fingerprint("ServiceDownProlonged", svc)
    assert alert["escalate"] is True
    after = _calls["written"][0]
    assert svc in after["reinvestigated_at"]

    # Tick 3 — the very next poll, still stuck, still past the threshold.
    _reset(collect, after)
    third = await _run()
    assert third["services_reinvestigated"] == 0
    assert _calls["spawned"] == []


async def test_service_confirmed_recently_is_not_reinvestigated():
    """The threshold itself: 2h of being stuck is not `restuck_hours`. Without
    it every confirmed service would re-alert on the tick after confirmation —
    exactly the 2-minute noise transition-only logic exists to avoid."""
    svc = "koyra-drwhome_drwhome-jobs"
    collect = {"ok": True, "nodes": {"baa": "Ready"}, "stuck": [svc], "error": ""}
    prior = {"nodes": {}, "stuck": [svc], "confirmed": [svc], "fail_count": 0}
    _reset(collect, prior)
    await _run()
    aged = _backdate(_calls["written"][0], "confirmed_at", svc, 2)
    _reset(collect, aged)
    result = await _run(InfraHeartbeatConfig(restuck_hours=24))
    assert result["services_reinvestigated"] == 0
    assert _calls["spawned"] == []


async def test_reinvestigation_repeats_after_the_dedup_window_expires():
    """The ratchet is a delay, not a permanent silence."""
    svc = "ollama_ollama-2"
    collect = {"ok": True, "nodes": {"baa": "Ready"}, "stuck": [svc], "error": ""}
    prior = {
        "nodes": {},
        "stuck": [svc],
        "confirmed": [svc],
        "confirmed_at": {},
        "reinvestigated_at": {},
        "fail_count": 0,
    }
    _reset(collect, prior)
    await _run()
    aged = _backdate(_calls["written"][0], "confirmed_at", svc, 60)
    _reset(collect, aged)
    await _run()
    once = _calls["written"][0]
    assert once["reinvestigated_at"][svc]

    # Another 30h with no recovery.
    aged_again = _backdate(once, "reinvestigated_at", svc, 30)
    _reset(collect, aged_again)
    again = await _run()
    assert again["services_reinvestigated"] == 1
    assert len(_calls["spawned"]) == 1
    assert _calls["spawned"][0]["labels"]["alertname"] == "ServiceDownProlonged"


async def test_restuck_hours_zero_disables_reinvestigation():
    svc = "koyracloud_redis"
    collect = {"ok": True, "nodes": {"baa": "Ready"}, "stuck": [svc], "error": ""}
    prior = {"nodes": {}, "stuck": [svc], "confirmed": [svc], "fail_count": 0}
    _reset(collect, prior)
    await _run(InfraHeartbeatConfig(restuck_hours=0))
    aged = _backdate(_calls["written"][0], "confirmed_at", svc, 240)
    _reset(collect, aged)
    result = await _run(InfraHeartbeatConfig(restuck_hours=0))
    assert result["services_reinvestigated"] == 0
    assert _calls["spawned"] == []


async def test_recovered_service_drops_its_clocks():
    """A service that recovers must lose confirmed_at/reinvestigated_at, or a
    later re-break would inherit a stale clock and re-investigate instantly."""
    svc = "svc_flappy"
    prior = {
        "nodes": {"baa": "Ready"},
        "stuck": [svc],
        "confirmed": [svc],
        "confirmed_at": {svc: "2026-01-01T00:00:00+00:00"},
        "reinvestigated_at": {svc: "2026-01-01T00:00:00+00:00"},
        "fail_count": 0,
    }
    _reset({"ok": True, "nodes": {"baa": "Ready"}, "stuck": [], "error": ""}, prior)
    await _run()
    written = _calls["written"][0]
    assert written["confirmed_at"] == {}
    assert written["reinvestigated_at"] == {}
