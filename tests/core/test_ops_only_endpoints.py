"""Contract tests for the endpoints deliberately kept without a UI consumer.

Issue #101 audited every route the admin SPA never calls. Four were deleted as
duplicates of a wired surface; the rest were kept and marked in their docstring
with the repo's ops-only marker. Nothing in the frontend exercises them, so
without these tests a refactor could break them and no one would notice until
someone reached for curl mid-incident.

Every assertion here checks the response **body** carries data this test put
into the database — a status-only assertion would still pass with the handler
deleted (FastAPI would 404, but so would a typo'd path), which is precisely the
failure mode this file exists to catch.
"""

from __future__ import annotations

import base64
import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from httpx import ASGITransport, AsyncClient

OPS_ONLY_MARKER = "intentionally curl/ops-only, no UI consumer"

AUTH_HEADERS = {"Authorization": f"Basic {base64.b64encode(b'admin:admin').decode()}"}


@pytest.fixture
def mock_knowledge():
    kc = AsyncMock()
    kc.get_stats.return_value = {"content_items": 7, "chunks": 41}
    return kc


@pytest.fixture
def mock_temporal():
    client = AsyncMock()
    handle = AsyncMock()
    handle.id = "manual-flow-deadbeef"
    client.start_workflow = AsyncMock(return_value=handle)
    return client


@pytest_asyncio.fixture(loop_scope="function")
async def app(test_settings, db_pool, mock_knowledge, mock_temporal):
    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: test_settings
    application.state.db_pool = db_pool
    application.state.knowledge_connector = mock_knowledge
    application.state.temporal_client = mock_temporal
    application.state.settings = test_settings
    return application


@pytest_asyncio.fixture(loop_scope="function")
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# --------------------------------------------------------------------------
# The marker itself — if someone drops the docstring line, the endpoint has
# silently lost the only thing that explains why it has no UI consumer.
# --------------------------------------------------------------------------


def _ops_only_handlers():
    from aegis.api.routes import integrations, knowledge, observability, settings

    return {
        "GET /api/observability/llm-calls": observability.list_llm_calls,
        "GET /api/observability/connector-calls": observability.list_connector_calls,
        "GET /api/admin/integrations/notification-budget": integrations.notification_budget,
        "POST /api/knowledge/ingest-drive": knowledge.ingest_drive,
        "GET /api/knowledge/health": knowledge.knowledge_health,
        "GET /api/settings/{key}": settings.get_setting,
    }


@pytest.mark.parametrize("route", sorted(_ops_only_handlers()))
def test_handler_carries_ops_only_marker(route):
    handler = _ops_only_handlers()[route]
    assert handler.__doc__ and OPS_ONLY_MARKER in handler.__doc__, (
        f"{route} lost its ops-only marker — either wire it to the UI or "
        f"restore the '{OPS_ONLY_MARKER}' docstring line (issue #101)."
    )


# --------------------------------------------------------------------------
# Live-body checks — each endpoint must return the row this test inserted.
# --------------------------------------------------------------------------


async def test_llm_calls_returns_the_inserted_row(client, db_pool):
    model = f"ops-only-{uuid.uuid4().hex[:8]}"
    await db_pool.execute(
        "INSERT INTO llm_calls (model, purpose, agent_id, input_tokens, "
        "output_tokens, latency_ms, status) VALUES ($1,'audit','sebas',11,22,33,'ok')",
        model,
    )
    resp = await client.get(
        f"/api/observability/llm-calls?model={model}", headers=AUTH_HEADERS
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1, rows
    assert rows[0]["model"] == model
    assert rows[0]["input_tokens"] == 11
    assert rows[0]["output_tokens"] == 22
    assert rows[0]["latency_ms"] == 33


async def test_connector_calls_returns_the_inserted_row(client, db_pool):
    connector = f"ops-only-{uuid.uuid4().hex[:8]}"
    await db_pool.execute(
        "INSERT INTO connector_calls (connector, action, status, latency_ms, error) "
        "VALUES ($1,'fetch','error',77,'boom')",
        connector,
    )
    resp = await client.get(
        f"/api/observability/connector-calls?connector={connector}", headers=AUTH_HEADERS
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1, rows
    assert rows[0]["action"] == "fetch"
    assert rows[0]["status"] == "error"
    assert rows[0]["error"] == "boom"


async def test_notification_budget_counts_sent_and_deferred(client, db_pool, test_settings):
    before = await client.get(
        "/api/admin/integrations/notification-budget", headers=AUTH_HEADERS
    )
    assert before.status_code == 200
    baseline = before.json()

    await db_pool.execute(
        "INSERT INTO notification_log (agent_id, log_event, sent) "
        "VALUES ('sebas','ops_only_test',true), ('sebas','ops_only_test',false)"
    )

    resp = await client.get(
        "/api/admin/integrations/notification-budget", headers=AUTH_HEADERS
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is test_settings.notification_budget_enabled
    assert body["daily_budget"] == test_settings.notification_daily_budget
    assert body["sent_today"] == baseline["sent_today"] + 1
    assert body["deferred_today"] == baseline["deferred_today"] + 1


async def test_knowledge_health_reports_stats_and_injection_count(client, db_pool):
    before = await client.get("/api/knowledge/health", headers=AUTH_HEADERS)
    assert before.status_code == 200
    baseline = before.json()["injection_log_30d"]

    await db_pool.execute(
        "INSERT INTO knowledge_injection_log (agent_id, source) VALUES ('sebas','ops_only_test')"
    )

    resp = await client.get("/api/knowledge/health", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    # kg_stats is echoed straight from the knowledge connector.
    assert body["kg_stats"] == {"content_items": 7, "chunks": 41}
    assert body["injection_log_30d"] == baseline + 1


async def test_settings_get_by_key_round_trips_the_value(client, db_pool):
    key = f"ops_only_{uuid.uuid4().hex[:8]}"
    await db_pool.execute(
        "INSERT INTO settings (key, value) VALUES ($1, '{\"mode\": \"curl\"}'::jsonb)", key
    )
    resp = await client.get(f"/api/settings/{key}", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == key
    assert body["value"] == {"mode": "curl"}


async def test_ingest_drive_returns_the_documented_reauth_error(client):
    """No Drive token for the account → 400 carrying the re-auth hint.

    The happy path needs real Google credentials; this pins the handler's own
    error contract, which is the part an operator actually reads.
    """
    missing = f"no-such-account-{uuid.uuid4().hex[:8]}"
    resp = await client.post(
        "/api/knowledge/ingest-drive",
        headers=AUTH_HEADERS,
        json={"folder_id": "folder123", "account": missing},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail.startswith("drive_ingest_failed:")
    assert missing in detail
    assert "re-authorized with Drive read access" in detail
    # The handler must resolve the account to a <account>.json token file.
    assert f"{missing}.json" in detail


# --------------------------------------------------------------------------
# The four routes deleted by #101 must stay gone — each duplicated a surface
# the SPA already calls, so a re-added twin is drift, not a feature.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/knowledge/jobs",
        "/api/knowledge/jobs/some-content-id",
        "/api/references/some-content-id",
        "/api/admin/todoist/clarify-log/1",
    ],
)
async def test_duplicate_routes_removed(client, path):
    resp = await client.get(path, headers=AUTH_HEADERS)
    assert resp.status_code == 404, f"{path} is back — see issue #101"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/admin/homelab/state"),
        ("post", "/api/admin/homelab/cert_radar/run"),
        ("get", "/api/admin/runbooks"),
        ("post", "/api/hub/events"),
        ("post", "/api/hub/service-state"),
        ("get", "/api/admin/service-state"),
        ("get", "/api/admin/infra-alert-routing"),
        ("get", "/api/admin/hub-settle-seconds"),
        ("get", "/api/admin/alert-remediation"),
    ],
)
async def test_the_infra_lane_routes_are_gone(client, method, path):
    """The infra lane moved to the DevOps vertical (a2-devops). Its routes must
    not come back: a POST or a GET is answered by nothing in v1."""
    resp = await getattr(client, method)(path, headers=AUTH_HEADERS)
    assert resp.status_code in (404, 405), f"{path} is back"
