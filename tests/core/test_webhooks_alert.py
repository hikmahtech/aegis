"""/api/webhooks/alert endpoint (Alertmanager/Grafana)."""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.api.routes.interactions import get_workflow_client
from aegis.api.routes.webhooks import ALERT_MAX_ALERTS_PER_REQUEST, ALERT_MAX_BODY_BYTES
from aegis.config import Settings
from httpx import ASGITransport, AsyncClient

_TEST_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "n8n_ui_url": "https://n8n.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
    "n8n_webhook_secret": "test-secret",
}


@pytest.fixture
def settings():
    return Settings(**_TEST_SETTINGS)


@pytest.fixture
def fake_hub(monkeypatch):
    """The problem hub without a database: one problem per fingerprint, a
    repeat of the same occurrence id is a duplicate, a resolved event with no
    problem is ignored, and nothing is projected. Lets the token / size / cap
    tests keep their mock pool."""
    from aegis.services.hub import IngestResult

    seen: set[str] = set()

    async def _ingest(pool, event, *, now=None):
        fp = event.payload.get("fingerprint") or "x"
        if event.kind == "resolved":
            return IngestResult(None, "ignored", "k")
        if event.external_id in seen:
            return IngestResult(f"prob-{fp}", "duplicate", "k")
        seen.add(event.external_id)
        return IngestResult(f"prob-{fp}", "created", "k", occurrences=1)

    async def _project(pool, problem_id, **kw):
        return {"task_id": None}

    monkeypatch.setattr("aegis.api.routes.webhooks.ingest_event", _ingest)
    monkeypatch.setattr("aegis.services.hub_project.project", _project)
    return seen


@pytest.fixture
def temporal_stub():
    handle = MagicMock()
    handle.id = "wf-alert-1"
    client = MagicMock()
    client.start_workflow = AsyncMock(return_value=handle)
    return client


def _mock_pool(fetchval_return=None):
    """Build a minimal mock asyncpg pool whose acquire() context manager yields
    a connection with configurable fetchval return value."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=fetchval_return)

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool, conn


@pytest_asyncio.fixture(loop_scope="function")
async def alert_client(settings, temporal_stub, fake_hub):
    """Mock-pool client over the fake hub."""
    pool, _ = _mock_pool()
    app = create_app(run_lifespan=False)
    app.state.db_pool = pool
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_workflow_client] = lambda: temporal_stub
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, temporal_stub


async def test_alertmanager_firing_spawns_flow(alert_client):
    c, temporal = alert_client
    payload = {
        "version": "4",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "HighCPU", "instance": "node-a", "severity": "critical"},
                "annotations": {"summary": "High CPU on node-a", "description": "95%"},
                "fingerprint": "am-test-1",
            },
        ],
    }
    resp = await c.post("/api/webhooks/alert", content=json.dumps(payload))
    assert resp.status_code == 200
    assert resp.json() == {"accepted": True, "started": 1, "skipped": 0, "dropped": 0}
    temporal.start_workflow.assert_awaited_once()
    call = temporal.start_workflow.call_args
    assert call.args[0] == "AlertInvestigationFlow"
    assert call.kwargs["id"] == "investigate-prob-am-test-1-1"
    assert call.kwargs["task_queue"] == "aegis-main"
    alert = call.args[1]
    assert alert["source"] == "alertmanager"
    assert alert["severity"] == "critical"
    assert alert["service"] == "node-a"
    assert alert["problem_id"] == "prob-am-test-1"


async def test_resolved_alert_skipped(alert_client):
    c, temporal = alert_client
    payload = {
        "alerts": [
            {"status": "resolved", "labels": {"alertname": "HighCPU"}, "fingerprint": "am-test-2"},
        ],
    }
    resp = await c.post("/api/webhooks/alert", content=json.dumps(payload))
    assert resp.status_code == 200
    assert resp.json() == {"accepted": True, "started": 0, "skipped": 1, "dropped": 0}
    temporal.start_workflow.assert_not_awaited()


async def test_duplicate_fingerprint_skipped(settings, temporal_stub, fake_hub):
    """The same occurrence (fingerprint + startsAt) twice: the hub reports a
    duplicate the second time and nothing starts."""
    pool_first, conn_first = _mock_pool()
    app = create_app(run_lifespan=False)
    app.state.db_pool = pool_first
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_workflow_client] = lambda: temporal_stub

    alert_payload = json.dumps(
        {
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "Mem", "instance": "node-a"},
                    "annotations": {"summary": "memory"},
                    "fingerprint": "am-dup-1",
                    "startsAt": "2026-09-07T10:00:00Z",
                }
            ]
        }
    ).encode()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r1 = await c.post("/api/webhooks/alert", content=alert_payload)

    assert r1.json()["started"] == 1

    # Second call: fetchval returns None (ON CONFLICT DO NOTHING → no row returned)
    pool_second, _ = _mock_pool(fetchval_return=None)
    app2 = create_app(run_lifespan=False)
    app2.state.db_pool = pool_second
    app2.dependency_overrides[get_settings] = lambda: settings
    app2.dependency_overrides[get_workflow_client] = lambda: temporal_stub

    async with AsyncClient(transport=ASGITransport(app=app2), base_url="http://test") as c2:
        r2 = await c2.post("/api/webhooks/alert", content=alert_payload)

    assert r2.json()["started"] == 0
    assert r2.json()["skipped"] == 1
    assert temporal_stub.start_workflow.await_count == 1


async def test_missing_fingerprint_synthesizes(alert_client):
    c, temporal = alert_client
    payload = {
        "alerts": [{"status": "firing", "labels": {"alertname": "X", "instance": "y"}}],
    }
    resp = await c.post("/api/webhooks/alert", content=json.dumps(payload))
    assert resp.json()["started"] == 1
    call = temporal.start_workflow.call_args
    assert "alertmanager:X:y" in call.kwargs["id"]


async def test_bad_json_returns_400(alert_client):
    c, _ = alert_client
    resp = await c.post("/api/webhooks/alert", content=b"not-json")
    assert resp.status_code == 400


async def test_multiple_alerts_mixed_status(settings, temporal_stub, fake_hub):
    """Two firing + one resolved: started=2, skipped=1."""
    pool, _ = _mock_pool()

    app = create_app(run_lifespan=False)
    app.state.db_pool = pool
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_workflow_client] = lambda: temporal_stub

    payload = {
        "alerts": [
            {"status": "firing", "labels": {"alertname": "A"}, "fingerprint": "m1"},
            {"status": "firing", "labels": {"alertname": "B"}, "fingerprint": "m2"},
            {"status": "resolved", "labels": {"alertname": "C"}, "fingerprint": "m3"},
        ],
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/webhooks/alert", content=json.dumps(payload))

    assert resp.json() == {"accepted": True, "started": 2, "skipped": 1, "dropped": 0}


async def test_bare_single_alert_dict_handled(alert_client):
    """Some ad-hoc integrations POST a single alert dict, not wrapped in `alerts`."""
    c, temporal = alert_client
    payload = {
        "status": "firing",
        "labels": {"alertname": "Z", "severity": "warning"},
        "annotations": {"summary": "test"},
        "fingerprint": "single-1",
    }
    resp = await c.post("/api/webhooks/alert", content=json.dumps(payload))
    assert resp.json()["started"] == 1
    call = temporal.start_workflow.call_args
    assert call.kwargs["id"] == "investigate-prob-single-1-1"


# ---------------------------------------------------------------------------
# Duplicate dedup with real Postgres
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="function")
async def alert_client_real_db(db_pool, settings, temporal_stub):
    app = create_app(run_lifespan=False)
    app.state.db_pool = db_pool
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_workflow_client] = lambda: temporal_stub
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, temporal_stub


async def test_firing_then_resolved_lands_on_one_problem(alert_client_real_db, db_pool):
    """A firing payload creates the problem and starts the investigation; the
    `status=resolved` payload for the same alert resolves that problem and
    starts nothing."""
    client, temporal = alert_client_real_db
    fp = f"am-{uuid.uuid4().hex[:10]}"
    firing = {
        "status": "firing",
        "labels": {"alertname": "HighCPU", "instance": fp},
        "fingerprint": fp,
        "startsAt": "2026-09-07T10:00:00Z",
    }
    resp = await client.post("/api/webhooks/alert", content=json.dumps({"alerts": [firing]}).encode())
    assert resp.json() == {"accepted": True, "started": 1, "skipped": 0, "dropped": 0}
    problem_id = temporal.start_workflow.call_args.args[1]["problem_id"]
    assert temporal.start_workflow.call_args.kwargs["id"] == f"investigate-{problem_id}-1"

    resolved = {**firing, "status": "resolved", "endsAt": "2026-09-07T10:30:00Z"}
    resp = await client.post("/api/webhooks/alert", content=json.dumps({"alerts": [resolved]}).encode())
    assert resp.json() == {"accepted": True, "started": 0, "skipped": 1, "dropped": 0}
    assert temporal.start_workflow.await_count == 1
    row = await db_pool.fetchrow(
        "SELECT status, occurrences FROM problems WHERE id = $1::uuid", problem_id
    )
    assert row["status"] == "resolved" and row["occurrences"] == 1


async def test_duplicate_fingerprint_real_db(alert_client_real_db):
    client, temporal = alert_client_real_db
    fp = f"am-real-dup-{uuid.uuid4().hex[:8]}"
    alert = {
        "status": "firing",
        "labels": {"alertname": "RealDup", "instance": fp},
        "annotations": {"summary": "real dup test"},
        "fingerprint": fp,
        "startsAt": "2026-09-07T10:00:00Z",
    }
    payload = json.dumps({"alerts": [alert]}).encode()
    r1 = await client.post("/api/webhooks/alert", content=payload)
    r2 = await client.post("/api/webhooks/alert", content=payload)
    assert r1.json()["started"] == 1
    assert r2.json()["started"] == 0
    assert r2.json()["skipped"] == 1
    assert temporal.start_workflow.await_count == 1


# ---------------------------------------------------------------------------
# Optional X-Alert-Token shared secret (#88)
#
# Alertmanager/Grafana don't sign payloads, so this endpoint has no vendor HMAC.
# Setting AEGIS_ALERT_WEBHOOK_SECRET requires a matching header; unset keeps the
# legacy open behaviour.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="function")
async def token_client(temporal_stub, fake_hub):
    """Client factory parametrised by the configured secret."""

    @asynccontextmanager
    async def _build(secret: str):
        settings = Settings(**{**_TEST_SETTINGS, "alert_webhook_secret": secret})
        pool, _ = _mock_pool()
        app = create_app(run_lifespan=False)
        app.state.db_pool = pool
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_workflow_client] = lambda: temporal_stub
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c

    return _build


_FIRING = json.dumps(
    {
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "TokenTest", "instance": "node-a"},
                "annotations": {"summary": "token test"},
                "fingerprint": "tok-1",
            }
        ]
    }
).encode()


async def test_alert_webhook_rejects_missing_token(token_client, temporal_stub):
    async with token_client("s3cret") as c:
        resp = await c.post("/api/webhooks/alert", content=_FIRING)
    assert resp.status_code == 401
    # Rejected before any flow is spawned — that's the whole point.
    temporal_stub.start_workflow.assert_not_awaited()


async def test_alert_webhook_rejects_wrong_token(token_client, temporal_stub):
    async with token_client("s3cret") as c:
        resp = await c.post(
            "/api/webhooks/alert", content=_FIRING, headers={"X-Alert-Token": "wrong"}
        )
    assert resp.status_code == 401
    temporal_stub.start_workflow.assert_not_awaited()


async def test_alert_webhook_accepts_good_token(token_client, temporal_stub):
    async with token_client("s3cret") as c:
        resp = await c.post(
            "/api/webhooks/alert", content=_FIRING, headers={"X-Alert-Token": "s3cret"}
        )
    assert resp.status_code == 200
    assert resp.json()["started"] == 1
    temporal_stub.start_workflow.assert_awaited()


async def test_alert_webhook_open_when_secret_unset(token_client):
    """Backward compatible: no secret configured = no token required."""
    async with token_client("") as c:
        resp = await c.post("/api/webhooks/alert", content=_FIRING)
    assert resp.status_code == 200
    assert resp.json()["started"] == 1


# --- Bearer auth (#304) ------------------------------------------------------
#
# The secret stayed blank in production because neither sender can set an
# arbitrary header on the pinned versions, but both speak Bearer: alertmanager
# via http_config.authorization, grafana via the webhook contact point's
# authorization_scheme/authorization_credentials. Accepting Bearer is what makes
# the secret settable without taking alerting down.


async def test_alert_accepts_bearer_token(token_client, temporal_stub):
    async with token_client("s3cret") as c:
        resp = await c.post(
            "/api/webhooks/alert",
            content=_FIRING,
            headers={"Authorization": "Bearer s3cret"},
        )
    assert resp.status_code == 200
    assert resp.json()["started"] == 1
    temporal_stub.start_workflow.assert_awaited()


async def test_bearer_scheme_is_case_insensitive(token_client):
    """"Bearer" is a case-insensitive token per RFC 7235."""
    async with token_client("s3cret") as c:
        resp = await c.post(
            "/api/webhooks/alert",
            content=_FIRING,
            headers={"Authorization": "bearer s3cret"},
        )
    assert resp.status_code == 200


async def test_x_alert_token_still_works(token_client):
    """The original header must keep working — do not break existing callers."""
    async with token_client("s3cret") as c:
        resp = await c.post(
            "/api/webhooks/alert", content=_FIRING, headers={"X-Alert-Token": "s3cret"}
        )
    assert resp.status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"Authorization": "Bearer wrong"}, id="wrong-credential"),
        pytest.param({"Authorization": "s3cret"}, id="no-scheme"),
        pytest.param({"Authorization": "Basic s3cret"}, id="wrong-scheme"),
        pytest.param({"Authorization": "Bearer"}, id="scheme-only"),
        pytest.param({"Authorization": "Bearer "}, id="empty-credential"),
        pytest.param({"X-Alert-Token": "wrong"}, id="wrong-x-alert-token"),
        pytest.param({}, id="no-header-at-all"),
    ],
)
async def test_alert_rejects_bad_credentials(token_client, temporal_stub, headers):
    """Every near-miss must 401 — adding a second accepted header must not
    accidentally widen what counts as authenticated."""
    async with token_client("s3cret") as c:
        resp = await c.post("/api/webhooks/alert", content=_FIRING, headers=headers)
    assert resp.status_code == 401
    temporal_stub.start_workflow.assert_not_awaited()


async def test_blank_secret_ignores_a_supplied_bearer(token_client):
    """With no secret configured the endpoint stays open (legacy default) and a
    stray Authorization header must not turn into a rejection."""
    async with token_client("") as c:
        resp = await c.post(
            "/api/webhooks/alert",
            content=_FIRING,
            headers={"Authorization": "Bearer anything"},
        )
    assert resp.status_code == 200


# --- Abuse caps (#304) -------------------------------------------------------
#
# /alert is the one webhook that can legitimately run unauthenticated (a blank
# `alert_webhook_secret` is a documented legacy default), and its work per
# request is unbounded: every array element can spawn an AlertInvestigationFlow,
# which costs LLM budget and writes to Todoist/Slack. These caps bound what a
# single request can do regardless of who sent it.


async def test_oversized_body_rejected_before_parsing(alert_client):
    """413 rather than buffering an arbitrarily large body into memory.

    The read is streamed, so this must fail on size alone — the payload is
    deliberately not valid JSON, so reaching the parser at all would be the bug.
    """
    c, temporal = alert_client
    resp = await c.post("/api/webhooks/alert", content=b"x" * (ALERT_MAX_BODY_BYTES + 1))
    assert resp.status_code == 413
    assert resp.json()["detail"] == "body_too_large"
    temporal.start_workflow.assert_not_awaited()


async def test_alert_fanout_is_capped(alert_client):
    """A huge array must not spawn one workflow per element."""
    c, temporal = alert_client
    over = ALERT_MAX_ALERTS_PER_REQUEST + 25
    payload = {
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": f"Flood{i}", "instance": "node-a"},
                "fingerprint": f"flood-{i}",
            }
            for i in range(over)
        ]
    }
    resp = await c.post("/api/webhooks/alert", content=json.dumps(payload))
    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] == ALERT_MAX_ALERTS_PER_REQUEST
    assert body["dropped"] == 25
    assert temporal.start_workflow.await_count == ALERT_MAX_ALERTS_PER_REQUEST


async def test_whole_cluster_outage_is_not_truncated():
    """The ceiling must clear a total-outage group, not just a normal one.

    This homelab loses power to half the cluster periodically, and alertmanager
    groups by (alertname, cluster, service) — so one `DockerServiceDown` event
    arrives as a single group carrying an entry per swarm service.

    Truncation keeps the FIRST N and each kept alert claims
    `ingest_idempotency`, so on the `group_interval` resend the same first N are
    skipped as duplicates and the dropped tail is never reached. Dropping here
    is permanent loss during the incident that matters most, which is why the
    cap is an abuse ceiling far above real traffic rather than a tight bound.
    """
    assert ALERT_MAX_ALERTS_PER_REQUEST >= 300, (
        "cap is too tight to survive a whole-cluster outage group; the dropped "
        "tail would be lost permanently, not retried. See the constant's comment."
    )


async def test_normal_sized_group_is_untouched(alert_client):
    """The cap must not clip a realistic Alertmanager group.

    Guards against a future cap set so tight it silently drops real alerts —
    the failure mode this endpoint already has too much of.
    """
    c, temporal = alert_client
    payload = {
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": f"Real{i}", "instance": "node-a"},
                "fingerprint": f"real-{i}",
            }
            for i in range(10)
        ]
    }
    resp = await c.post("/api/webhooks/alert", content=json.dumps(payload))
    assert resp.status_code == 200
    assert resp.json() == {"accepted": True, "started": 10, "skipped": 0, "dropped": 0}
