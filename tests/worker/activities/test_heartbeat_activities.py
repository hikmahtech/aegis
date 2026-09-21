from unittest.mock import AsyncMock

import pytest
from aegis_worker.activities.homelab import HomelabActivities


def _act(homelab=None, db_pool=None, **kw):
    return HomelabActivities(db_pool=db_pool, homelab=homelab, delivery=AsyncMock(), **kw)


@pytest.mark.asyncio
async def test_collect_infra_state_merges_nodes_and_stuck_services():
    homelab = AsyncMock()
    homelab.list_nodes.return_value = {
        "ok": True,
        "data": [
            {"hostname": "baa", "status": "Ready", "availability": "Active", "manager": "Leader"},
            {"hostname": "noon", "status": "Down", "availability": "Active", "manager": ""},
        ],
    }
    homelab.list_services.return_value = {
        "ok": True,
        "data": [
            {"name": "koyracloud_order-finder", "replicas_actual": 0, "replicas_desired": 1},
            {"name": "aegis_core", "replicas_actual": 1, "replicas_desired": 1},
            {"name": "batch_job", "replicas_actual": 0, "replicas_desired": 0},
        ],
    }
    state = await _act(homelab=homelab).collect_infra_state()
    assert state["ok"] is True
    assert state["nodes"] == {"baa": "Ready", "noon": "Down"}
    assert state["stuck"] == ["koyracloud_order-finder"]  # desired>0 only


@pytest.mark.asyncio
async def test_collect_infra_state_node_failure_is_not_ok():
    homelab = AsyncMock()
    homelab.list_nodes.return_value = {"ok": False, "error": "ssh timeout", "data": None}
    state = await _act(homelab=homelab).collect_infra_state()
    assert state["ok"] is False
    assert "ssh timeout" in state["error"]


@pytest.mark.asyncio
async def test_read_heartbeat_state_defaults_when_unset():
    pool = AsyncMock()
    pool.fetchval.return_value = None
    state = await _act(db_pool=pool).read_heartbeat_state()
    assert state == {
        "nodes": {},
        "stuck": [],
        "confirmed": [],
        "fail_count": 0,
    }


@pytest.mark.asyncio
async def test_read_heartbeat_state_keeps_a_row_that_still_carries_the_old_clocks():
    """The per-service `confirmed_at` / `reinvestigated_at` clocks behind the
    #138 re-investigate path are gone — the hub answers that question now
    (`stale_stuck_problems`). A state row still carrying them must read back
    without complaint; the extra keys simply go unread."""
    pool = AsyncMock()
    pool.fetchval.return_value = {
        "nodes": {"baa": "Ready"},
        "stuck": ["a"],
        "confirmed": ["a"],
        "confirmed_at": {"a": "2026-09-01T00:00:00+00:00"},
        "reinvestigated_at": {},
        "fail_count": 0,
    }
    state = await _act(db_pool=pool).read_heartbeat_state()
    assert state["confirmed"] == ["a"] and state["stuck"] == ["a"]
    assert state["nodes"] == {"baa": "Ready"}


@pytest.mark.asyncio
async def test_write_then_read_roundtrip_shape():
    pool = AsyncMock()
    act = _act(db_pool=pool)
    await act.write_heartbeat_state({"nodes": {"baa": "Ready"}, "stuck": [], "confirmed": [], "fail_count": 0})
    sql = pool.execute.await_args.args[0]
    assert "infra_heartbeat_state" in pool.execute.await_args.args
    assert "ON CONFLICT" in sql


@pytest.mark.asyncio
async def test_ping_deadman_noop_without_url():
    result = await _act().ping_deadman()
    assert result == {"pinged": False}


@pytest.mark.asyncio
async def test_read_heartbeat_state_returns_fresh_containers_each_call():
    pool = AsyncMock()
    pool.fetchval.return_value = None
    act = _act(db_pool=pool)
    first = await act.read_heartbeat_state()
    first["stuck"].append("phantom_service")
    first["nodes"]["ghost"] = "Down"
    second = await act.read_heartbeat_state()
    assert second["stuck"] == []
    assert second["nodes"] == {}


def test_services_on_node_keeps_the_replicated_services_with_a_live_task():
    """#633: what a node that went down was carrying. A task there keeps its
    last state (`Running …`) because the node cannot report a change; the
    managers already asked for it to shut down, so the desired state says
    nothing. History rows and global services are left out."""
    from aegis_worker.activities.homelab import services_on_node

    tasks = [
        # Pinned there, still reads Running while the node is down.
        {"name": "postgres_postgres.1", "current_state": "Running 3 hours ago", "desired_state": "Shutdown"},
        {"name": "clickhouse_clickhouse.1", "current_state": "Running 3 hours ago", "desired_state": "Running"},
        # A second replica of the same service: one name.
        {"name": "clickhouse_clickhouse.2", "current_state": "Starting 3 hours ago", "desired_state": "Running"},
        {"name": "dagster_daemon.1", "current_state": "Preparing 3 hours ago", "desired_state": "Running"},
        # History: not on the node now.
        {"name": "loki_loki.1", "current_state": "Shutdown 2 days ago", "desired_state": "Shutdown"},
        {"name": "old_app.1", "current_state": "Failed 5 hours ago", "desired_state": "Shutdown"},
        {"name": "gone_app.1", "current_state": "Orphaned 2 days ago", "desired_state": "Shutdown"},
        # Global: named `<service>.<node id>`, runs on every node.
        {"name": "promtail_promtail.x9z8y7w6v5u4t3s2r1q0p9o8n", "current_state": "Running 3 hours ago",
         "desired_state": "Shutdown"},
        # The table format's history indent must not become part of a name.
        {"name": " \\_ litellm_db.1", "current_state": "Running 3 hours ago", "desired_state": "Running"},
        {"name": "", "current_state": "Running", "desired_state": "Running"},
    ]
    assert services_on_node(tasks) == [
        "clickhouse_clickhouse",
        "dagster_daemon",
        "litellm_db",
        "postgres_postgres",
    ]


@pytest.mark.asyncio
async def test_node_services_asks_once_and_raises_on_failure():
    homelab = AsyncMock()
    homelab.node_ps.return_value = {
        "ok": True,
        "data": [{"name": "postgres_postgres.1", "current_state": "Running 1 hour ago"}],
    }
    assert await _act(homelab=homelab).node_services("lam") == ["postgres_postgres"]
    homelab.node_ps.assert_awaited_once_with("lam")
    homelab.node_ps.return_value = {"ok": False, "error": "timeout", "data": None}
    with pytest.raises(RuntimeError, match="timeout"):
        await _act(homelab=homelab).node_services("lam")
    # No connector: nothing to hold.
    assert await _act(homelab=None).node_services("lam") == []
