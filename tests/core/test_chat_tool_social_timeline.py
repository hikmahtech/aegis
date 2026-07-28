"""Tests for the `social_timeline` chat tool (Postiz post timeline)."""

from __future__ import annotations

import json

import pytest
from aegis.services.chat import AGENT_TOOL_SETS, CHAT_TOOLS, TOOL_EXECUTORS, ToolContext


class _FakeConnector:
    """Stands in for SocialConnector — records the window it was asked for."""

    def __init__(self, posts=None, error: Exception | None = None, **_kwargs) -> None:
        self.posts = posts or []
        self.error = error
        self.closed = False
        self.window: tuple[str, str] | None = None

    async def list_posts_window(self, start_iso: str, end_iso: str) -> list[dict]:
        self.window = (start_iso, end_iso)
        if self.error:
            raise self.error
        return self.posts

    async def close(self) -> None:
        self.closed = True


def _patch_connector(monkeypatch, fake: _FakeConnector) -> None:
    import aegis.connectors.social as social_mod

    monkeypatch.setattr(social_mod, "SocialConnector", lambda **kwargs: fake)


_POSTS = [
    {
        "id": "p1",
        "content": "<p>Older published post</p>",
        "publishDate": "2026-07-03T23:34:00.000Z",
        "releaseURL": "https://linkedin.example/1",
        "state": "PUBLISHED",
        "integration": {"providerIdentifier": "linkedin-page", "name": "Hikmah Technologies"},
    },
    {
        "id": "p2",
        "content": "Queued post",
        "publishDate": "2026-08-01T09:00:00.000Z",
        "releaseURL": None,
        "state": "QUEUE",
        "integration": {"providerIdentifier": "x", "name": "arshad"},
    },
]


def test_social_timeline_schema_registered():
    names = {t["function"]["name"] for t in CHAT_TOOLS}
    assert "social_timeline" in names


def test_social_timeline_has_executor():
    assert "social_timeline" in TOOL_EXECUTORS


def test_sebas_can_call_social_timeline():
    assert "social_timeline" in AGENT_TOOL_SETS["sebas"]


async def test_social_timeline_normalizes_posts(monkeypatch):
    fake = _FakeConnector(posts=_POSTS)
    _patch_connector(monkeypatch, fake)

    result = json.loads(await TOOL_EXECUTORS["social_timeline"](None, {}, ToolContext()))

    assert result["count"] == 2
    assert result["truncated"] is False
    # Newest first — the queued August post leads.
    assert [p["state"] for p in result["posts"]] == ["QUEUE", "PUBLISHED"]
    published = result["posts"][1]
    assert published["text"] == "Older published post"  # HTML stripped
    assert published["channel"] == "Hikmah Technologies"
    assert published["platform"] == "linkedin-page"
    assert published["url"] == "https://linkedin.example/1"
    assert fake.closed is True


async def test_social_timeline_filters_by_state(monkeypatch):
    _patch_connector(monkeypatch, _FakeConnector(posts=_POSTS))

    result = json.loads(
        await TOOL_EXECUTORS["social_timeline"](None, {"state": "queue"}, ToolContext())
    )

    assert result["count"] == 1
    assert result["posts"][0]["state"] == "QUEUE"


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({}, (14, 14)),
        ({"days_back": 3, "days_ahead": 0}, (3, 0)),
        ({"days_back": 9999, "days_ahead": -5}, (90, 0)),
        ({"days_back": "nonsense"}, (14, 14)),
    ],
)
async def test_social_timeline_clamps_window(monkeypatch, args, expected):
    _patch_connector(monkeypatch, _FakeConnector())

    result = json.loads(await TOOL_EXECUTORS["social_timeline"](None, args, ToolContext()))

    assert (result["window"]["days_back"], result["window"]["days_ahead"]) == expected


async def test_social_timeline_reports_connector_failure(monkeypatch):
    fake = _FakeConnector(error=RuntimeError("postiz not configured"))
    _patch_connector(monkeypatch, fake)

    result = json.loads(await TOOL_EXECUTORS["social_timeline"](None, {}, ToolContext()))

    assert "postiz not configured" in result["error"]
    assert fake.closed is True
