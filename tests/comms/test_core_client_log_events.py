"""`SlackCoreClient` emits the same five log event names it always has.

`_post`, `_patch` and `_get` now share one `_request` body. A log event name is
an interface — Loki queries and dashboards select on it — so sharing the body
must not share the name. This pins all five against the shape each caller had
before, including the one that logs nothing: a GET's non-200 was silent, and
turning it into a warning would put a line in the log on every poll of a thing
that is legitimately not there.
"""

from __future__ import annotations

import httpx
import pytest
import structlog
from aegis_comms.slack_inbound import SlackCoreClient

pytestmark = pytest.mark.asyncio


class _Settings:
    core_url = "http://core.test"
    api_key = "k"
    admin_username = "u"
    admin_password = "p"


def _client() -> SlackCoreClient:
    return SlackCoreClient(_Settings())


def _events(logs: list[dict]) -> list[str]:
    return [entry["event"] for entry in logs]


@pytest.fixture
def non_200(monkeypatch):
    async def _request(self, method, url, **kwargs):
        return httpx.Response(500, text="boom", request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.AsyncClient, "request", _request)


@pytest.fixture
def transport_error(monkeypatch):
    async def _request(self, method, url, **kwargs):
        raise httpx.ConnectTimeout("")

    monkeypatch.setattr(httpx.AsyncClient, "request", _request)


async def test_a_non_ok_response_keeps_each_callers_own_event(non_200):
    client = _client()
    with structlog.testing.capture_logs() as logs:
        assert await client._post("/api/chat", {}) is None
    assert _events(logs) == ["slack_core_post_non_2xx"]

    with structlog.testing.capture_logs() as logs:
        assert await client._patch("/api/x", {}) is None
    assert _events(logs) == ["slack_core_patch_non_200"]

    # A GET's non-200 was silent and stays silent.
    with structlog.testing.capture_logs() as logs:
        assert await client._get("/api/x") is None
    assert _events(logs) == []


async def test_a_transport_failure_keeps_each_callers_own_event(transport_error):
    client = _client()
    for call, expected in (
        (client._post("/api/chat", {}), "slack_core_post_failed"),
        (client._patch("/api/x", {}), "slack_core_patch_failed"),
        (client._get("/api/x"), "slack_core_get_failed"),
    ):
        with structlog.testing.capture_logs() as logs:
            assert await call is None
        assert _events(logs) == [expected]


async def test_a_post_still_reports_why_it_failed(non_200):
    """The `error_sink` contract #296 added: a caller telling a human what
    broke needs Core's own 500 body and the status behind it."""
    sink: dict = {}
    assert await _client()._post("/api/chat", {}, error_sink=sink) is None
    assert sink["status_code"] == 500
    assert "boom" in sink["reason"]


async def test_a_post_accepts_202(monkeypatch):
    """The async dispatch lane answers 202; only `_post` treats it as ok."""

    async def _request(self, method, url, **kwargs):
        return httpx.Response(202, json={"queued": True}, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.AsyncClient, "request", _request)
    client = _client()
    assert await client._post("/api/chat", {}) == {"queued": True}
    assert await client._patch("/api/x", {}) is None
