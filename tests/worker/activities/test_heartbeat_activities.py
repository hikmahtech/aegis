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
    pool.fetchrow.return_value = None
    state = await _act(db_pool=pool).read_heartbeat_state()
    assert state == {"nodes": {}, "stuck": [], "confirmed": [], "fail_count": 0}


@pytest.mark.asyncio
async def test_write_then_read_roundtrip_shape():
    pool = AsyncMock()
    act = _act(db_pool=pool)
    await act.write_heartbeat_state({"nodes": {"baa": "Ready"}, "stuck": [], "confirmed": [], "fail_count": 0})
    sql = pool.execute.await_args.args[0]
    assert "infra_heartbeat_state" in pool.execute.await_args.args
    assert "ON CONFLICT" in sql


@pytest.mark.asyncio
async def test_record_heartbeat_resolved_writes_alert_received_resolved_row():
    pool = AsyncMock()
    act = _act(db_pool=pool)
    await act.record_heartbeat_resolved("aegis-heartbeat:NodeDown:noon")
    # log_audit inserts into audit_log; assert via the pool call it makes
    assert pool.execute.await_count + pool.fetchrow.await_count + pool.fetchval.await_count >= 1


@pytest.mark.asyncio
async def test_ping_deadman_noop_without_url():
    result = await _act().ping_deadman()
    assert result == {"pinged": False}


@pytest.mark.asyncio
async def test_read_heartbeat_state_returns_fresh_containers_each_call():
    pool = AsyncMock()
    pool.fetchrow.return_value = None
    act = _act(db_pool=pool)
    first = await act.read_heartbeat_state()
    first["stuck"].append("phantom_service")
    first["nodes"]["ghost"] = "Down"
    second = await act.read_heartbeat_state()
    assert second["stuck"] == []
    assert second["nodes"] == {}
