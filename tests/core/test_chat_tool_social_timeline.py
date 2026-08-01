"""Tests for the `social_timeline` chat tool (Postiz post timeline)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

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


# --- Channel coverage: "which channels am I posting to?" -----------------
#
# Prod bug: the executor sorted furthest-future-first and then cut the list to
# fit a byte budget, blind to which channels the survivors covered. A real
# 30-back/30-ahead call kept 13 of 75 posts, all from one two-week slice; every
# Bluesky and Medium post fell off, and the agent reported a correctly
# configured channel as "not in the timeline at all". Compounding it, the row
# carried only `integration.name`, which is NOT unique — dev.to and the personal
# LinkedIn share one display name, so two platforms arrived as one label.


def _iso(days: float) -> str:
    """A Postiz publishDate `days` away from now (negative = past)."""
    return (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:00.000Z")


def _post(idx: int, platform: str, name: str, days: float, state: str = "PUBLISHED") -> dict:
    """A realistically fat Postiz post: long body, long release URL."""
    return {
        "id": f"p{idx}",
        "content": f"<p>{'Building in public, one commit at a time. ' * 8}</p>",
        "publishDate": _iso(days),
        "releaseURL": f"https://www.linkedin.com/feed/update/urn:li:share:74789542444657786{idx:03d}/",
        "state": state,
        "integration": {"providerIdentifier": platform, "name": name},
    }


def _realistic_window() -> list[dict]:
    """~70 posts over six channels, shaped like the call that exposed the bug.

    The bulk sits near today on one channel and eats the whole byte budget;
    Bluesky, Medium, dev.to and the personal LinkedIn all live out at the window
    edges, so no sampling strategy keeps them.
    """
    posts = [_post(i, "linkedin-page", "Hikmah Technologies", -5 + i * 0.15) for i in range(60)]
    posts += [_post(100 + i, "bluesky", "hikmah.bsky.social", -29 + i) for i in range(5)]
    posts += [_post(200 + i, "mastodon", "@hikmah@mastodon.social", -24 + i) for i in range(3)]
    # Same human, two platforms — indistinguishable without providerIdentifier.
    posts += [_post(300 + i, "devto", "Mohammed Arshad Ansari", -21 + i) for i in range(2)]
    posts += [_post(400 + i, "linkedin", "Mohammed Arshad Ansari", -19 + i) for i in range(2)]
    posts += [_post(500 + i, "medium", "Mohammed Arshad Ansari", 27 + i, "QUEUE") for i in range(2)]
    return posts


async def _timeline(monkeypatch, posts: list[dict], args: dict | None = None) -> tuple[str, dict]:
    _patch_connector(monkeypatch, _FakeConnector(posts=posts))
    raw = await TOOL_EXECUTORS["social_timeline"](
        None, args or {"days_back": 30, "days_ahead": 30}, ToolContext()
    )
    return raw, json.loads(raw)


async def test_channels_in_window_is_complete_even_when_posts_are_truncated(monkeypatch):
    """THE BUG. Every channel in the window must be reported, sample or no sample."""
    _, result = await _timeline(monkeypatch, _realistic_window())

    assert result["truncated"] is True, "fixture must force a cut or it proves nothing"
    sampled = {p["platform"] for p in result["posts"]}
    assert "bluesky" not in sampled, "fixture must drop Bluesky from the sample"
    assert "medium" not in sampled, "fixture must drop Medium from the sample"

    channels = result["channels_in_window"]
    assert set(channels) == {"linkedin-page", "bluesky", "mastodon", "devto", "linkedin", "medium"}
    assert channels["bluesky"] == {
        "name": "hikmah.bsky.social",
        "posts": 5,
        "queued": 0,
        "published": 5,
        "next": None,
    }
    assert channels["medium"]["posts"] == channels["medium"]["queued"] == 2
    assert channels["medium"]["next"] is not None, "a future post must surface as `next`"
    assert channels["linkedin-page"]["posts"] == 60
    assert sum(c["posts"] for c in channels.values()) == result["total_in_window"] == 74


async def test_platform_comes_from_provider_identifier(monkeypatch):
    _, result = await _timeline(monkeypatch, _POSTS)

    by_state = {p["state"]: p for p in result["posts"]}
    assert by_state["PUBLISHED"]["platform"] == "linkedin-page"
    assert by_state["QUEUE"]["platform"] == "x"


async def test_channels_sharing_a_display_name_stay_distinguishable(monkeypatch):
    """dev.to and the personal LinkedIn both render as "Mohammed Arshad Ansari"."""
    _, result = await _timeline(monkeypatch, _realistic_window())

    channels = result["channels_in_window"]
    assert channels["devto"]["name"] == channels["linkedin"]["name"] == "Mohammed Arshad Ansari"
    assert channels["devto"]["posts"] == 2
    assert channels["linkedin"]["posts"] == 2
    # Keying on the display name would have collapsed devto+linkedin+medium into
    # one entry and lost two platforms.
    assert len({c["name"] for c in channels.values()}) < len(channels)


async def test_truncated_flag_tracks_whether_rows_were_dropped(monkeypatch):
    _, cut = await _timeline(monkeypatch, _realistic_window())
    assert cut["truncated"] is True
    assert cut["count"] < cut["total_in_window"]

    _, whole = await _timeline(monkeypatch, _POSTS)
    assert whole["truncated"] is False
    assert whole["count"] == whole["total_in_window"] == 2


def _many_channel_window(n_channels: int) -> list[dict]:
    """The same shape on a Postiz account that has grown to `n_channels`.

    `channels_in_window` is complete by design, so it scales with the account.
    Those bytes have to come back out of the posts budget or the total blows
    past 4096 — at which point `_smart_subset` drops `posts` wholesale, the
    exact failure PR #147 was written to prevent.
    """
    return [
        _post(c * 10 + i, f"platform-{c:02d}", f"A Channel Name {c:02d}", -3 + c * 0.2 + i * 0.1)
        for c in range(n_channels)
        for i in range(6)
    ]


@pytest.mark.parametrize(
    ("label", "posts"),
    [
        ("six channels, as deployed", _realistic_window()),
        ("twelve channels, after growth", _many_channel_window(12)),
    ],
)
async def test_result_fits_the_tool_result_cap(monkeypatch, label, posts):
    """Sample + summary together must clear 4096, or `_smart_subset` eats `posts`."""
    from aegis.services.chat import _truncate_result

    raw, result = await _timeline(monkeypatch, posts)

    size = len(raw.encode())
    assert size <= 4096, f"{label}: result is {size} bytes — the truncator would drop `posts`"
    assert _truncate_result(raw) == raw, f"{label}: truncator rewrote the result"
    # `posts` leads and `channels_in_window` is second, so any future overflow
    # sheds metadata rather than the two keys that carry the answer.
    assert list(result)[:2] == ["posts", "channels_in_window"]
    assert result["posts"], f"{label}: posts must survive; dropping them is the regression"
    expected = {p["integration"]["providerIdentifier"] for p in posts}
    assert set(result["channels_in_window"]) == expected, f"{label}: a channel went missing"
