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
    assert result["total_in_window"] == 2
    # Newest first — the queued August post leads.
    assert [p["state"] for p in result["posts"]] == ["QUEUE", "PUBLISHED"]
    published = result["posts"][1]
    assert published["text"] == "Older published post"  # HTML stripped
    assert published["channel"] == "Hikmah Technologies"
    assert published["date"] == "2026-07-03 23:34"
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


async def test_social_timeline_result_survives_the_tool_result_truncator(monkeypatch):
    """The regression that made this tool useless in prod.

    `_truncate_result` shrinks an over-budget dict by keeping its first N KEYS,
    so a fat payload doesn't come back trimmed — `posts` vanishes entirely and
    the model sees only metadata. Sebas then re-called the tool five times
    hunting for data no window would return. The executor must therefore fit
    the byte budget itself, whatever Postiz hands back.
    """
    from aegis.services.chat import _truncate_result

    many = [
        {
            "id": f"p{i}",
            "content": f"<p>{'post body ' * 60}</p>",  # ~600 chars each, well over budget
            "publishDate": f"2026-07-{(i % 28) + 1:02d}T09:00:00.000Z",
            "releaseURL": f"https://linkedin.example/feed/update/urn:li:share:74789542444657786{i:02d}",
            "state": "PUBLISHED",
            "integration": {"providerIdentifier": "linkedin-page", "name": "Hikmah Technologies"},
        }
        for i in range(80)
    ]
    _patch_connector(monkeypatch, _FakeConnector(posts=many))

    raw = await TOOL_EXECUTORS["social_timeline"](None, {}, ToolContext())

    # Untouched by the truncator — which is the whole point.
    assert _truncate_result(raw) == raw
    result = json.loads(raw)
    assert result["posts"], "posts must survive; dropping them is the bug"
    assert result["count"] < result["total_in_window"] == 80
    # Still newest-first after the budget cut, not an arbitrary slice.
    dates = [p["date"] for p in result["posts"]]
    assert dates == sorted(dates, reverse=True)


async def test_social_timeline_keeps_one_post_even_if_oversized(monkeypatch):
    """A single monster post must not yield an empty timeline."""
    _patch_connector(
        monkeypatch,
        _FakeConnector(
            posts=[
                {
                    "id": "big",
                    "content": "x" * 50_000,
                    "publishDate": "2026-07-10T09:00:00.000Z",
                    "state": "PUBLISHED",
                    "integration": {"name": "Hikmah Technologies"},
                }
            ]
        ),
    )

    result = json.loads(await TOOL_EXECUTORS["social_timeline"](None, {}, ToolContext()))

    assert result["count"] == 1
    assert len(result["posts"][0]["text"]) <= 140


async def test_social_timeline_reports_connector_failure(monkeypatch):
    fake = _FakeConnector(error=RuntimeError("postiz not configured"))
    _patch_connector(monkeypatch, fake)

    result = json.loads(await TOOL_EXECUTORS["social_timeline"](None, {}, ToolContext()))

    assert "postiz not configured" in result["error"]
    assert fake.closed is True
