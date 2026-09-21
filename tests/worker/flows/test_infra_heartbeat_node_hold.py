"""InfraHeartbeatFlow and a node that goes down on its own (#633).

Two changes. A NodeDown carries the replicated services that had a task on
the node (one `docker node ps`, asked on the tick the node goes down), so the
hub can hold back their problems while it is down; it is ingested before the
tick's own service alerts. And only a node that went not ready within
`outage_recent_hours` counts toward `outage_min_nodes`: a node that has been
off for days no longer turns the next failure into a cluster outage.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from temporalio import workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from tests.worker.flows import test_infra_heartbeat_flow as hb

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.infra_heartbeat import (
        PATCH_NODE_SERVICES,
        PATCH_OUTAGE_RECENT,
        InfraHeartbeatConfig,
        InfraHeartbeatFlow,
        _hb_fingerprint,
    )

_OUTAGE_FP = _hb_fingerprint("ClusterOutage", "")
LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC).isoformat()


def _prior(nodes: dict, *, since: dict | None = None, stuck: list | None = None, outage=False) -> dict:
    return {
        "nodes": nodes,
        "stuck": stuck or [],
        "confirmed": [],
        "fail_count": 0,
        "outage": outage,
        "not_ready_since": since or {},
    }


def _collect(nodes: dict, stuck: list | None = None) -> dict:
    return {"ok": True, "nodes": nodes, "stuck": stuck or [], "error": ""}


def _node_alert(node: str) -> dict:
    fp = _hb_fingerprint("NodeDown", node)
    [alert] = [a for a in hb._calls.get("ingested_alerts", []) if a["fingerprint"] == fp]
    return alert


# --- the node's services ------------------------------------------------------


async def test_a_nodedown_carries_the_services_that_had_a_task_there():
    hb._reset(_collect({"baa": "Ready", "lam": "Down"}), _prior({"baa": "Ready", "lam": "Ready"}))
    hb._state["placement"] = {"lam": ["clickhouse_clickhouse", "postgres_postgres"]}
    result = await hb._run()
    assert hb._calls["node_services"] == ["lam"]
    alert = _node_alert("lam")
    assert alert["services"] == ["clickhouse_clickhouse", "postgres_postgres"]
    assert "clickhouse_clickhouse, postgres_postgres" in alert["description"]
    assert result["alerts_spawned"] == 1


async def test_the_nodedown_is_ingested_before_the_ticks_service_alerts():
    """Otherwise the first tick still raises every service on the node before
    the hub knows the node explains them."""
    prior = _prior({"baa": "Ready", "lam": "Ready"}, stuck=["postgres_postgres"])
    hb._reset(_collect({"baa": "Ready", "lam": "Down"}, stuck=["postgres_postgres"]), prior)
    hb._state["placement"] = {"lam": ["postgres_postgres"]}
    await hb._run()
    order = hb._calls["order"]
    node = order.index(f"ingest:{_hb_fingerprint('NodeDown', 'lam')}")
    service = order.index(f"ingest:{_hb_fingerprint('DockerServiceDown', 'postgres_postgres')}")
    assert order.index("node_services:lam") < node < service


async def test_steady_down_and_quiet_nodes_ask_nothing():
    nodes = {"baa": "Ready", "lam": "Down", "pop": "Down"}
    hb._reset(_collect(nodes), _prior({"baa": "Ready", "lam": "Down", "pop": "Ready"}))
    await hb._run(InfraHeartbeatConfig(quiet_nodes=["pop"]))
    # lam was already down (no transition); pop leaves on purpose.
    assert "node_services" not in hb._calls


async def test_a_failed_lookup_still_raises_the_nodedown():
    hb._reset(_collect({"baa": "Ready", "lam": "Down"}), _prior({"baa": "Ready", "lam": "Ready"}))
    hb._state["placement"] = {"lam": ApplicationError("docker node ps: timeout", non_retryable=True)}
    result = await hb._run()
    alert = _node_alert("lam")
    assert "services" not in alert
    assert result["alerts_spawned"] == 1


# --- only recent nodes count toward an outage ----------------------------------


async def test_a_node_down_for_days_does_not_make_the_next_failure_an_outage():
    prior = _prior({"baa": "Ready", "noon": "Down", "lam": "Ready"}, since={"noon": LONG_AGO})
    hb._reset(_collect({"baa": "Ready", "noon": "Down", "lam": "Down"}), prior)
    result = await hb._run()
    assert _OUTAGE_FP not in hb._calls.get("ingested", [])
    assert result["outage"] is False
    assert result["nodes_not_ready"] == 2 and result["nodes_counted"] == 1
    # lam is its own NodeDown problem.
    assert _hb_fingerprint("NodeDown", "lam") in hb._calls["ingested"]
    since = hb._calls["written"][-1]["not_ready_since"]
    assert since["noon"] == LONG_AGO
    assert datetime.fromisoformat(since["lam"]) > datetime.fromisoformat(LONG_AGO)
    assert "baa" not in since

    # 0 counts every node however long it has been down (the #630 rule).
    hb._reset(_collect({"baa": "Ready", "noon": "Down", "lam": "Down"}), prior)
    result = await hb._run(InfraHeartbeatConfig(outage_recent_hours=0))
    assert result["outage"] is True


async def test_two_recent_nodes_are_an_outage_and_the_old_one_is_named():
    prior = _prior({"noon": "Down", "lam": "Ready", "wow": "Ready"}, since={"noon": LONG_AGO})
    hb._reset(_collect({"noon": "Down", "lam": "Down", "wow": "Down"}), prior)
    result = await hb._run()
    assert result["outage"] is True and result["nodes_counted"] == 2
    [outage] = [a for a in hb._calls["ingested_alerts"] if a["fingerprint"] == _OUTAGE_FP]
    assert "lam, wow" in outage["description"]
    assert "Not counted, not ready for over 6h: noon" in outage["description"]


async def test_an_outage_ends_once_its_nodes_have_been_down_too_long():
    """The heartbeat's side of the six-hour cap: the nodes that made the
    outage stop counting, so it resolves while they are still down. Each is
    still its own NodeDown problem, which keeps holding its own services."""
    since = {"lam": LONG_AGO, "wow": LONG_AGO}
    nodes = {"baa": "Ready", "lam": "Down", "wow": "Down"}
    hb._reset(_collect(nodes), _prior(nodes, since=since, outage=True))
    result = await hb._run()
    assert _OUTAGE_FP in hb._calls["resolved"]
    assert result["outage"] is False


async def test_a_node_back_up_leaves_the_map():
    prior = _prior({"baa": "Ready", "lam": "Down"}, since={"lam": LONG_AGO, "gone": LONG_AGO})
    hb._reset(_collect({"baa": "Ready", "lam": "Ready"}), prior)
    await hb._run()
    assert hb._calls["written"][-1]["not_ready_since"] == {}


def test_outage_recent_hours_is_read_from_activities_config():
    from aegis_worker.registry import FLOWS

    spec = next(s for s in FLOWS if s.flow is InfraHeartbeatFlow)
    row = {"agent_id": "pandoras-actor", "_settings": {}}
    assert spec.schedule_config({**row, "config": {}}).outage_recent_hours == 6
    assert spec.schedule_config({**row, "config": {"outage_recent_hours": 24}}).outage_recent_hours == 24
    assert spec.schedule_config({**row, "config": {"outage_recent_hours": ""}}).outage_recent_hours == 6


# --- a tick in flight across the deploy ----------------------------------------


async def test_a_tick_from_before_633_replays(monkeypatch):
    """A heartbeat in flight across the deploy asked for no placement and
    counted every not-ready node. Both steps are behind `workflow.patched`,
    so the new flow replays its history."""
    real = workflow.patched
    ours = {PATCH_NODE_SERVICES, PATCH_OUTAGE_RECENT}
    prior = _prior({"noon": "Down", "lam": "Ready", "baa": "Ready"}, since={"noon": LONG_AGO})
    hb._reset(_collect({"noon": "Down", "lam": "Down", "baa": "Ready"}), prior)
    hb._state["placement"] = {"lam": ["postgres_postgres"]}
    monkeypatch.setattr(workflow, "patched", lambda pid: False if pid in ours else real(pid))
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
        result = await handle.result()
        history = await handle.fetch_history()
    monkeypatch.undo()
    # The premise: this tick really ran the code from before #633 — no
    # placement asked, and the long-down node still counted.
    assert "node_services" not in hb._calls
    assert result["outage"] is True
    await Replayer(workflows=[InfraHeartbeatFlow]).replay_workflow(history)


async def test_a_new_tick_replays_too():
    """And a history recorded by the new code replays on it (the patch
    markers are in the history)."""
    prior = _prior({"noon": "Down", "lam": "Ready"}, since={"noon": LONG_AGO})
    hb._reset(_collect({"noon": "Down", "lam": "Down"}), prior)
    hb._state["placement"] = {"lam": ["postgres_postgres"]}
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
    assert hb._calls["node_services"] == ["lam"]
    await Replayer(workflows=[InfraHeartbeatFlow]).replay_workflow(history)
