"""apply_restart_approval — the InteractionFlow post_resolve hook."""

from __future__ import annotations

import pytest
from aegis_worker.activities.agent_task import AgentTaskActivities


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # The convergence poll awaits asyncio.sleep(4) up to 5x — stub it so the
    # suite doesn't actually burn up to 20s per test (test_alert_investigation.py
    # uses this same monkeypatch shape for the kimi-poll activity).
    from unittest.mock import AsyncMock

    monkeypatch.setattr("aegis_worker.activities.agent_task.asyncio.sleep", AsyncMock())


class _Recorder:
    def __init__(self, healthy_after_restart: bool):
        # Mirrors InfraOpsActivities' surface so the hook's poll loop works.
        self.healthy_after = healthy_after_restart
        self.restarted: list[str] = []
        self.completed: list[str] = []
        self.parked: list[str] = []
        self.notes: list[str] = []

    async def restart_service(self, service_name: str) -> dict:
        self.restarted.append(service_name)
        return {"ok": True, "detail": "ok"}

    async def service_health(self, service_name: str) -> dict:
        return {"found": True, "healthy": self.healthy_after, "detail": "1/1"}


def _act(rec: _Recorder) -> AgentTaskActivities:
    # `infra_ops` is a normal collaborator field, so the fake drops straight in —
    # no private-attribute injection.
    act = AgentTaskActivities(db_pool=None, infra_ops=rec)
    async def _complete(task_id: str) -> dict:
        rec.completed.append(task_id)
        return {"completed": True}
    async def _park(task_id: str, reason: str) -> dict:
        rec.parked.append(task_id)
        return {"parked": True}
    async def _comment(task_id: str, agent_id: str, body: str) -> dict:
        rec.notes.append(body)
        return {"ok": True}
    act.complete_task = _complete                        # type: ignore[assignment]
    act.park_task = _park                                # type: ignore[assignment]
    act.comment = _comment                               # type: ignore[assignment]
    return act


_META = {"task_id": "tr-1", "service": "redis_redis", "agent_id": "pandoras-actor"}


async def test_approve_restarts_and_completes_when_service_recovers():
    rec = _Recorder(healthy_after_restart=True)
    result = await _act(rec).apply_restart_approval("i1", {"value": "approve"}, _META)
    assert result == {"applied": "approved"}
    assert rec.restarted == ["redis_redis"]
    assert rec.completed == ["tr-1"]
    assert rec.parked == []


async def test_approve_parks_when_service_still_broken_after_restart():
    rec = _Recorder(healthy_after_restart=False)
    await _act(rec).apply_restart_approval("i1", {"value": "approve"}, _META)
    assert rec.restarted == ["redis_redis"]
    assert rec.completed == []
    assert rec.parked == ["tr-1"]


async def test_skip_parks_without_restarting():
    rec = _Recorder(healthy_after_restart=True)
    result = await _act(rec).apply_restart_approval("i1", {"value": "skip"}, _META)
    assert result == {"applied": "skipped"}
    assert rec.restarted == []
    assert rec.parked == ["tr-1"]


async def test_unknown_choice_takes_no_action():
    rec = _Recorder(healthy_after_restart=True)
    result = await _act(rec).apply_restart_approval("i1", {"value": "???"}, _META)
    assert result == {"applied": "none"}
    assert rec.restarted == []


async def test_missing_task_id_takes_no_action():
    rec = _Recorder(healthy_after_restart=True)
    result = await _act(rec).apply_restart_approval("i1", {"value": "approve"}, {})
    assert result == {"applied": "none"}
    assert rec.restarted == []
