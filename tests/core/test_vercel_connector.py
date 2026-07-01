"""VercelConnector — read-only HTTP client for Vercel REST."""

import pytest
import respx
from aegis.connectors.vercel import VercelConnector
from httpx import Response

BASE = "https://api.vercel.com"


def _make(token: str = "vcp_test", team: str = "team_abc"):
    return VercelConnector(token=token, team_id=team)


@pytest.mark.asyncio
@respx.mock
async def test_get_project_success():
    route = respx.get(f"{BASE}/v9/projects/drwhome").mock(
        return_value=Response(
            200,
            json={"id": "prj_abc", "name": "drwhome", "framework": "nextjs"},
        )
    )
    c = _make()
    result = await c.get_project("drwhome")
    assert route.call_count == 1
    assert "teamId=team_abc" in str(route.calls[0].request.url)
    assert result["id"] == "prj_abc"
    assert result["name"] == "drwhome"
    await c.close()


@pytest.mark.asyncio
@respx.mock
async def test_get_project_404_returns_error_dict():
    respx.get(f"{BASE}/v9/projects/nonexistent").mock(
        return_value=Response(404, json={"error": {"code": "not_found"}})
    )
    c = _make()
    result = await c.get_project("nonexistent")
    assert result["error"] == "project_not_found"
    assert result["project"] == "nonexistent"
    await c.close()


@pytest.mark.asyncio
async def test_get_project_no_token_short_circuits():
    c = _make(token="")
    result = await c.get_project("drwhome")
    assert result == {"error": "vercel_token_not_configured"}


@pytest.mark.asyncio
@respx.mock
async def test_list_deployments_uses_app_param_for_name():
    route = respx.get(f"{BASE}/v6/deployments").mock(
        return_value=Response(
            200,
            json={
                "deployments": [
                    {
                        "uid": "dpl_1",
                        "name": "drwhome",
                        "url": "drwhome-abc.vercel.app",
                        "state": "READY",
                        "created": 1779000000000,
                        "meta": {"githubCommitRef": "main", "githubCommitSha": "abc123def456"},
                    }
                ]
            },
        )
    )
    c = _make()
    result = await c.list_deployments("drwhome", limit=5)
    assert route.call_count == 1
    qs = str(route.calls[0].request.url)
    assert "app=drwhome" in qs
    assert "limit=5" in qs
    assert "projectId" not in qs
    assert result["count"] == 1
    d = result["deployments"][0]
    assert d["uid"] == "dpl_1"
    assert d["state"] == "READY"
    assert d["meta_branch"] == "main"
    assert d["meta_sha"] == "abc123d"  # 7-char trim
    # Timestamps come out as ISO-8601 UTC (Z suffix), not raw epoch ms.
    assert d["created_at"] == "2026-05-17T06:40:00Z"
    assert "created" not in d  # legacy field removed
    await c.close()


@pytest.mark.asyncio
@respx.mock
async def test_list_deployments_uses_project_id_when_prj_prefixed():
    route = respx.get(f"{BASE}/v6/deployments").mock(
        return_value=Response(200, json={"deployments": []})
    )
    c = _make()
    await c.list_deployments("prj_abc123", limit=5)
    qs = str(route.calls[0].request.url)
    assert "projectId=prj_abc123" in qs
    assert "app=" not in qs
    await c.close()


@pytest.mark.asyncio
@respx.mock
async def test_list_deployments_state_filter_normalizes_case():
    route = respx.get(f"{BASE}/v6/deployments").mock(
        return_value=Response(200, json={"deployments": []})
    )
    c = _make()
    await c.list_deployments("drwhome", state="error")  # lowercase
    qs = str(route.calls[0].request.url)
    assert "state=ERROR" in qs
    await c.close()


@pytest.mark.asyncio
async def test_list_deployments_rejects_unknown_state():
    c = _make()
    result = await c.list_deployments("drwhome", state="WEIRD")
    assert result["error"].startswith("invalid_state")
    assert "READY" in result["allowed"]


@pytest.mark.asyncio
@respx.mock
async def test_list_deployments_since_hours_sets_epoch_ms():
    route = respx.get(f"{BASE}/v6/deployments").mock(
        return_value=Response(200, json={"deployments": []})
    )
    c = _make()
    await c.list_deployments("drwhome", since_hours=24)
    qs = str(route.calls[0].request.url)
    assert "since=" in qs
    # Confirm the since value is a recent epoch ms (within last day or so)
    import re

    m = re.search(r"since=(\d+)", qs)
    assert m
    since_ms = int(m.group(1))
    import time as _t

    now_ms = int(_t.time() * 1000)
    assert now_ms - 25 * 3600 * 1000 < since_ms < now_ms - 23 * 3600 * 1000
    await c.close()


@pytest.mark.asyncio
@respx.mock
async def test_get_deployment_surfaces_error_fields():
    respx.get(f"{BASE}/v13/deployments/dpl_err").mock(
        return_value=Response(
            200,
            json={
                "id": "dpl_err",
                "name": "drwhome",
                "url": "drwhome-x.vercel.app",
                "readyState": "ERROR",
                "createdAt": 1779000000000,
                "errorCode": "BUILD_FAILED",
                "errorMessage": "Command failed: npm run build",
                "errorStep": "build",
                "meta": {
                    "githubCommitRef": "feature/x",
                    "githubCommitSha": "deadbeefcafe",
                    "githubCommitMessage": "fix: oops",
                },
            },
        )
    )
    c = _make()
    result = await c.get_deployment("dpl_err")
    assert result["readyState"] == "ERROR"
    assert result["errorCode"] == "BUILD_FAILED"
    assert result["errorMessage"] == "Command failed: npm run build"
    assert result["meta_sha"] == "deadbee"
    assert result["meta_message"] == "fix: oops"
    # createdAt → ISO-8601 string under created_at (matches list_deployments shape).
    assert result["created_at"] == "2026-05-17T06:40:00Z"
    assert "createdAt" not in result
    await c.close()


@pytest.mark.asyncio
@respx.mock
async def test_get_build_logs_trims_and_returns_count():
    respx.get(f"{BASE}/v3/deployments/dpl_1/events").mock(
        return_value=Response(
            200,
            json=[
                {"type": "stdout", "text": "Build started\n", "created": 1779000000000},
                {"type": "stderr", "text": "warning: foo\n", "created": 1779000000001},
                {"type": "stderr", "text": "error: bar\n", "created": 1779000000002},
            ],
        )
    )
    c = _make()
    result = await c.get_build_logs("dpl_1", limit=10)
    assert result["count"] == 3
    assert result["errors_only"] is False
    assert result["events"][0]["text"] == "Build started"  # rstrip
    # Timestamps come out as ISO-8601 UTC.
    assert result["events"][0]["created_at"] == "2026-05-17T06:40:00Z"
    assert "created" not in result["events"][0]
    await c.close()


@pytest.mark.asyncio
@respx.mock
async def test_get_build_logs_errors_only_filters_stderr():
    respx.get(f"{BASE}/v3/deployments/dpl_1/events").mock(
        return_value=Response(
            200,
            json=[
                {"type": "stdout", "text": "Build started", "created": 1},
                {"type": "stderr", "text": "warning: foo", "created": 2},
                {"type": "stderr", "text": "error: bar", "created": 3},
            ],
        )
    )
    c = _make()
    result = await c.get_build_logs("dpl_1", limit=10, errors_only=True)
    assert result["count"] == 2
    assert result["errors_only"] is True
    assert all(e["type"] == "stderr" for e in result["events"])
    await c.close()


@pytest.mark.asyncio
async def test_get_build_logs_no_token_short_circuits():
    c = _make(token="")
    result = await c.get_build_logs("dpl_1")
    assert result == {"error": "vercel_token_not_configured"}
