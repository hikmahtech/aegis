"""Tests for the `list_social_channels` chat tool (#184).

The defect it exists to close: nothing was backed by `social_accounts`, so an
agent asked "which social channels can you post to?" had to infer the answer
from `social_timeline` — a view of POSTS in a window, not of channels. On
2026-08-01 that inference told the operator "no Bluesky channel appears in the
timeline at all" while Bluesky was connected, mirrored, and had 5 posts queued.

Rows here are prefixed `zzls-` so nothing perturbs another test sharing the
same xdist-worker database.
"""

from __future__ import annotations

import json

import pytest_asyncio
from aegis.services.chat import AGENT_TOOL_SETS, CHAT_TOOLS, TOOL_EXECUTORS, ToolContext

_SETTINGS_KEYS = ["social_platform_labels", "social_publishing_enabled"]


@pytest_asyncio.fixture(loop_scope="function")
async def channels_env(db_pool):
    """A clean `social_accounts` + the two settings rows the tool reads."""
    async with db_pool.acquire() as conn:
        originals = {
            r["key"]: r["value"]
            for r in await conn.fetch(
                "SELECT key, value FROM settings WHERE key = ANY($1::text[])", _SETTINGS_KEYS
            )
        }
        await conn.execute("DELETE FROM social_outbox")
        await conn.execute("DELETE FROM social_accounts")
    yield db_pool
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM social_outbox")
        await conn.execute("DELETE FROM social_accounts")
        for key in _SETTINGS_KEYS:
            if key in originals:
                await conn.execute(
                    "INSERT INTO settings (key, value) VALUES ($1, $2) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    key,
                    originals[key],
                )
            else:
                await conn.execute("DELETE FROM settings WHERE key = $1", key)


async def _set(pool, key: str, value) -> None:
    await pool.execute(
        "INSERT INTO settings (key, value) VALUES ($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        key,
        value,
    )


async def _postiz_channel(pool, platform: str, label: str, integration_id: str = "int") -> None:
    await pool.execute(
        "INSERT INTO social_accounts (platform, label, meta) VALUES ($1, $2, $3)",
        platform,
        label,
        {"postiz_integration_id": integration_id, "via": "postiz"},
    )


async def _run(pool, args: dict | None = None) -> tuple[str, dict]:
    raw = await TOOL_EXECUTORS["list_social_channels"](pool, args or {}, ToolContext())
    return raw, json.loads(raw)


# --- registration --------------------------------------------------------


def test_schema_registered():
    assert "list_social_channels" in {t["function"]["name"] for t in CHAT_TOOLS}


def test_has_executor():
    assert "list_social_channels" in TOOL_EXECUTORS


def test_sebas_holds_it_in_the_seed_default():
    assert "list_social_channels" in AGENT_TOOL_SETS["sebas"]


def test_description_steers_away_from_social_timeline():
    """The wrong 2026-08-01 answer came from using `social_timeline` for a
    question about CHANNELS — the schema has to say so, or the model repeats it."""
    schema = next(
        t["function"] for t in CHAT_TOOLS if t["function"]["name"] == "list_social_channels"
    )
    description = schema["description"]
    assert "social_timeline" in description
    assert "connected" in description


# --- the answer itself ---------------------------------------------------


async def test_lists_every_connected_channel(channels_env):
    """THE BUG: a connected channel must be reported whether or not it has any
    posts. `social_accounts` is the only source that can say so."""
    await _postiz_channel(channels_env, "bluesky", "zzls-hikmahtech", "int-bsky")
    await _postiz_channel(channels_env, "linkedin-page", "zzls-hikmah", "int-li")
    await _set(channels_env, "social_platform_labels", {"bluesky": "bluesky"})

    _, result = await _run(channels_env)

    by_platform = {c["platform"]: c for c in result["channels"]}
    assert set(by_platform) == {"bluesky", "linkedin-page"}
    assert result["count"] == result["total"] == 2
    assert result["truncated"] is False
    assert by_platform["bluesky"] == {
        "platform": "bluesky",
        "channel": "zzls-hikmahtech",
        "via": "postiz",
        "todoist_label": "bluesky",
    }


async def test_native_and_postiz_transports_are_distinguished(channels_env):
    await _postiz_channel(channels_env, "medium", "zzls-arshad", "int-med")
    await channels_env.execute(
        "INSERT INTO social_accounts (platform, label, meta) VALUES ('x', 'zzls-native', '{}')"
    )

    _, result = await _run(channels_env)

    via = {c["platform"]: c["via"] for c in result["channels"]}
    assert via == {"medium": "postiz", "x": "native"}


async def test_labeled_platforms_with_no_account_are_reported(channels_env):
    """The #183 state, made visible: `x` and `youtube` are mapped in prod with
    no account behind them, so a task labelled for them cannot publish."""
    await _postiz_channel(channels_env, "bluesky", "zzls-hikmahtech", "int-bsky")
    await _set(
        channels_env,
        "social_platform_labels",
        {"bluesky": "bluesky", "x": "x", "youtube": "youtube"},
    )

    _, result = await _run(channels_env)

    assert result["labeled_but_not_connected"] == {"x": "x", "youtube": "youtube"}
    assert [c["platform"] for c in result["channels"]] == ["bluesky"]


async def test_no_labeled_gap_when_every_mapped_platform_is_connected(channels_env):
    """Proves the assertion above tracks the DB rather than always reporting a
    gap — with the account present the same label map yields nothing."""
    await _postiz_channel(channels_env, "bluesky", "zzls-hikmahtech", "int-bsky")
    await _postiz_channel(channels_env, "x", "zzls-x", "int-x")
    await _set(channels_env, "social_platform_labels", {"bluesky": "bluesky", "x": "x"})

    _, result = await _run(channels_env)

    assert result["labeled_but_not_connected"] == {}


async def test_empty_mirror_is_an_explicit_empty_answer(channels_env):
    """An install with nothing connected must say so rather than error — but
    this only proves anything because the tests above seeded rows and saw them."""
    await _set(channels_env, "social_platform_labels", {"x": "x"})

    _, result = await _run(channels_env)

    assert result["channels"] == []
    assert result["count"] == result["total"] == 0
    assert result["labeled_but_not_connected"] == {"x": "x"}


async def test_reports_the_publishing_kill_switch(channels_env):
    await _postiz_channel(channels_env, "bluesky", "zzls-hikmahtech", "int-bsky")

    await _set(channels_env, "social_publishing_enabled", True)
    _, on = await _run(channels_env)
    assert on["publishing_enabled"] is True

    await _set(channels_env, "social_publishing_enabled", False)
    _, off = await _run(channels_env)
    assert off["publishing_enabled"] is False


# --- the 4096-byte tool-result cap ---------------------------------------


async def test_result_survives_the_tool_result_truncator(channels_env):
    """`_truncate_result` shrinks an over-budget dict by keeping its first N
    KEYS — over the cap `channels` is dropped WHOLESALE and the model gets only
    metadata. That is the exact failure mode PR #185 fixed on the sibling tool;
    this one must fit its own budget on a big account.
    """
    from aegis.services.chat import _truncate_result

    long_name = "हिकमह टेक्नोलॉजीज़ प्राइवेट लिमिटेड 🚀✨ बहुत लंबा नाम"
    for i in range(40):
        await _postiz_channel(channels_env, f"platform-{i:02d}", f"zzls-{long_name} {i:02d}", f"i{i}")

    raw, result = await _run(channels_env)

    size = len(raw.encode())
    assert size <= 4096, f"result is {size} bytes — the truncator would drop `channels`"
    assert _truncate_result(raw) == raw, "truncator rewrote the result"
    assert result["channels"], "channels must survive; dropping them is the bug"
    # Honest about the cut instead of silently implying the list is complete.
    assert result["total"] == 40
    assert result["truncated"] is True
    assert result["count"] < result["total"]
    assert list(result)[0] == "channels"
