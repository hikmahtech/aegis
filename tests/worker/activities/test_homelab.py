# tests/worker/activities/test_homelab.py
from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis_worker.activities.homelab import HomelabActivities
from aegis_worker.flows.service_drift import _compute_drift_inline


def _make_homelab_act(delivery):
    return HomelabActivities(db_pool=None, homelab=None, delivery=delivery)


@pytest.mark.asyncio
async def test_notify_drift_sends_str_message_to_pandora_topic():
    """Regression for PR #25x: notify_drift used to pass a dict to send_system_event,
    which serialized into `{"text": {...}}` and failed pydantic validation
    server-side with 422. The fix routes through send_message(agent_id="pandoras-actor",
    message=str)."""
    delivery = AsyncMock()
    delivery.channel = "slack"
    act = _make_homelab_act(delivery)
    await act.notify_drift(
        {
            "service_name": "aegis_core",
            "drift_type": "replicas",
            "expected": {"desired": 1},
            "actual": {"actual": 0},
            "severity": "critical",
            "detected_at": "2026-05-25T20:00:00+00:00",
        }
    )
    delivery.send_message.assert_awaited_once()
    kwargs = delivery.send_message.await_args.kwargs
    assert kwargs["agent_id"] == "pandoras-actor"
    assert isinstance(kwargs["message"], str)
    assert "aegis_core" in kwargs["message"]


@pytest.mark.asyncio
async def test_notify_payloads_validate_against_delivery_request_schema():
    """Pydantic validation of the actual JSON body sent to /api/deliver/message.
    Catches the class of bugs where a dict gets passed where a str is required.

    The schema is a local mirror of aegis_comms.__main__.DeliveryRequest —
    the worker test CI installs core+worker only, not comms, so we can't
    import the real class. Keep the fields in sync if the receiver changes.
    """
    from pydantic import BaseModel

    class DeliveryRequest(BaseModel):
        text: str
        chat_id: int = 0
        agent_id: str = "sebas"
        system_event: bool = False
        parse_mode: str = "HTML"
        reply_markup: dict | None = None

    captured_bodies: list[dict] = []

    async def fake_send_message(*, agent_id, message, chat_id=0, keyboard=None):
        body = {"text": message, "chat_id": chat_id, "agent_id": agent_id}
        if keyboard:
            body["reply_markup"] = keyboard
        captured_bodies.append(body)

    delivery = AsyncMock(side_effect=None)
    delivery.channel = "slack"
    delivery.send_message = fake_send_message

    act = _make_homelab_act(delivery)
    await act.notify_drift(
        {
            "service_name": "aegis_core",
            "drift_type": "oom_exit",
            "expected": {"exit": "clean"},
            "actual": {"task_id": "t1", "error": "non-zero exit (137)"},
            "severity": "critical",
        }
    )
    await act.notify_cert_alert(
        {"domain": "example.com", "threshold": 7, "days": 5, "not_after": "2026-06-01T00:00:00"}
    )

    assert len(captured_bodies) == 2
    for body in captured_bodies:
        DeliveryRequest.model_validate(body)


def _audit_pool(prev_size=None):
    """Mock db_pool whose `async with .acquire() as conn` yields a conn with
    fetchval (previous size) + execute (health insert)."""
    conn = AsyncMock()
    conn.fetchval.return_value = prev_size
    conn.execute.return_value = None
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)  # asyncpg: acquire() sync-returns an async CM
    return pool, conn


@pytest.mark.asyncio
async def test_notify_pr_event_tracked_repo_notifies():
    pool, conn = _audit_pool()
    conn.fetchval.return_value = 1  # repo found in resources
    delivery = AsyncMock()
    delivery.channel = "slack"
    act = HomelabActivities(db_pool=pool, homelab=None, delivery=delivery)
    out = await act.notify_pr_event(
        {"repo": "youruser/aegis", "number": 42, "title": "x", "author": "a",
         "action": "opened", "url": "u"}
    )
    assert out["notified"] is True
    assert out["repo"] == "youruser/aegis"


@pytest.mark.asyncio
async def test_notify_pr_event_untracked_repo_skipped():
    pool, conn = _audit_pool()
    conn.fetchval.return_value = None  # not in resources
    act = HomelabActivities(db_pool=pool, homelab=None, delivery=AsyncMock())
    out = await act.notify_pr_event({"repo": "stranger/repo", "action": "opened"})
    assert out["notified"] is False
    assert out["reason"] == "untracked_repo"


def test_compute_drift_detects_replica_shortfall_and_oom():
    """Exercises the pure drift-detection function used by the workflow."""
    collected = {
        "services": [
            {
                "name": "aegis_core",
                "stack": "aegis",
                "replicas_actual": 0,
                "replicas_desired": 1,
                "image": "youruser/aegis-core:abc",
                "mode": "replicated",
            }
        ],
        "ps_map": {
            "aegis_core": [
                {
                    "task_id": "t1",
                    "current_state": "Shutdown 2h ago",
                    "desired_state": "Shutdown",
                    "error": "task: non-zero exit (137)",
                    "image": "youruser/aegis-core:abc",
                    "node": "node-a",
                },
            ]
        },
    }
    drifts = _compute_drift_inline(collected, today="2026-04-16")
    types = [d["drift_type"] for d in drifts]
    assert "replicas" in types
    assert "oom_exit" in types


def test_compute_drift_ignores_ancient_oom_when_service_healthy():
    """Stale OOM history should NOT produce a drift when the current task is Running."""
    collected = {
        "services": [
            {
                "name": "aegis_temporal",
                "stack": "aegis",
                "replicas_actual": 1,
                "replicas_desired": 1,
                "image": "temporalio/auto-setup:latest",
                "mode": "replicated",
            }
        ],
        "ps_map": {
            "aegis_temporal": [
                # Current healthy task (docker service ps returns newest first)
                {
                    "task_id": "t0",
                    "current_state": "Running 5 weeks ago",
                    "desired_state": "Running",
                    "error": "",
                    "image": "temporalio/auto-setup:latest",
                    "node": "mgr-3",
                },
                # Historical OOM — should be ignored
                {
                    "task_id": "t1",
                    "current_state": "Failed 5 weeks ago",
                    "desired_state": "Shutdown",
                    "error": "task: non-zero exit (137)",
                    "image": "temporalio/auto-setup:latest",
                    "node": "mgr-3",
                },
            ]
        },
    }
    drifts = _compute_drift_inline(collected, today="2026-04-16")
    assert drifts == []


def test_compute_drift_flags_crashlooping_service():
    """Most-recent task with OOM error → oom_exit drift fires."""
    collected = {
        "services": [
            {
                "name": "loki_promtail",
                "stack": "loki",
                "replicas_actual": 0,
                "replicas_desired": 1,
                "image": "grafana/promtail:latest",
                "mode": "replicated",
            }
        ],
        "ps_map": {
            "loki_promtail": [
                {
                    "task_id": "t0",
                    "current_state": "Failed 2 minutes ago",
                    "desired_state": "Running",
                    "error": "task: non-zero exit (137)",
                    "image": "grafana/promtail:latest",
                    "node": "node-a",
                },
            ]
        },
    }
    drifts = _compute_drift_inline(collected, today="2026-04-16")
    types = [d["drift_type"] for d in drifts]
    assert "replicas" in types
    assert "oom_exit" in types
