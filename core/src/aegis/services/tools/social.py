"""Social chat tools — the Postiz post timeline and the connected-channel list.

Both are read-only and both are byte-budgeted: `_truncate_result` shrinks an
over-budget dict by keeping its first N KEYS, so a payload that overflows comes
back as metadata with the answer dropped. Each tool therefore fits its own
result to the budget rather than letting the truncator do it.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from itertools import zip_longest

import asyncpg

from aegis.errors import error_text
from aegis.services.tools.base import ToolContext
from aegis.services.tools.registry import aegis_tool

_HTML_TAG_RE = re.compile(r"<[^>]+>")
# Stay clear of _truncate_result's 4096-byte cap. That truncator shrinks a
# dict by keeping its first N KEYS, so an over-budget result here doesn't come
# back trimmed — the `posts` list is dropped wholesale and the model gets only
# the metadata (it then re-calls with narrower windows, hunting for the data
# that no window will ever produce). Fitting the budget ourselves is the only
# way to keep the payload; the row count adapts to how long the posts are.
# `posts` is only a SAMPLE, so it can never answer "which channels am I posting
# to?" — the complete answer is `channels_in_window`, computed over every row
# before the budget cut. The drop from 3400 to 2900 is what funds it.
_SOCIAL_TIMELINE_BUDGET = 2900
_SOCIAL_TIMELINE_TEXT = 140
# Nominal size of `channels_in_window`; anything beyond it is taken back out of
# the posts budget so the whole result still clears 4096 bytes.
_SOCIAL_TIMELINE_SUMMARY_ALLOWANCE = 500
# ...but that clawback can only shrink `posts`, which floors at one row, so an
# UNCAPPED roll-up still blows the 4096 cap on a big account — and then
# `_smart_subset` keeps the leading `posts` key and drops `channels_in_window`
# entirely, killing the complete-coverage guarantee on exactly the account that
# needs it. Measured: 12 channels with long Devanagari+emoji names = 4360 bytes
# (json.dumps escapes each char to \uXXXX, 6 bytes; emoji 12), 40 = 13279.
# So the roll-up is capped by BYTES, not by channel count — the tail folds into
# one `+K more` aggregate, which keeps the post totals complete either way.
_SOCIAL_TIMELINE_CHANNEL_CAP = 1400
_SOCIAL_TIMELINE_CHANNEL_NAME = 40


@aegis_tool
async def _exec_social_timeline(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    days_back: int = 14,
    days_ahead: int = 14,
    state: str | None = None,
) -> str:
    """The social-media post timeline from Postiz: what was published, what is still queued/scheduled, on which channel, with the live post URL. Use when the user asks about posts, the posting schedule, what went out on a given channel, or what is lined up next. `posts` is a sample and may be partial (`truncated` says so); `channels_in_window` is the per-channel roll-up over the WHOLE window, keyed by platform — answer 'which channels am I posting to?' from it, never from `posts`. It accounts for every post in the window, but on an account with many channels only the busiest are listed individually and the rest are summed into a single `+K more` entry; say so rather than implying the named ones are all.

    Args:
        days_back: How far back to look, in days (default 14, max 90).
        days_ahead: How far ahead to look, in days (default 14, max 90).
        state: Optional Postiz state filter, e.g. PUBLISHED, QUEUE, DRAFT, ERROR.

    Returns:
        Reads Postiz directly (not `social_outbox`) so posts authored in the
        Postiz UI show up alongside the ones AEGIS published.
    """
    from aegis.connectors.social import SocialConnector

    def _clamp(raw: object, default: int) -> int:
        if raw is None or raw == "":
            return default
        try:
            value = int(raw)  # 0 is meaningful ("no future posts"), so don't `or default`
        except (TypeError, ValueError):
            return default
        return min(max(value, 0), 90)

    days_back = _clamp(days_back, 14)
    days_ahead = _clamp(days_ahead, 14)
    state = (state or "").strip().upper()

    now = datetime.now(UTC)
    connector = SocialConnector(db_pool=pool, settings=ctx.settings)
    try:
        posts = await connector.list_posts_window(
            (now - timedelta(days=days_back)).isoformat(),
            (now + timedelta(days=days_ahead)).isoformat(),
        )
    except Exception as exc:  # noqa: BLE001 — surface as a tool result, not a chat crash
        return json.dumps({"error": error_text(exc, 300)})
    finally:
        await connector.close()

    rows = []
    for post in posts:
        post_state = str(post.get("state") or "")
        if state and post_state.upper() != state:
            continue
        integration = post.get("integration") or {}
        text = _HTML_TAG_RE.sub("", str(post.get("content") or "")).strip()
        channel = integration.get("name")
        if isinstance(channel, str):
            channel = channel[:_SOCIAL_TIMELINE_CHANNEL_NAME]
        rows.append(
            {
                "date": str(post.get("publishDate") or "")[:16].replace("T", " "),
                "state": post_state,
                # The display name is NOT unique — dev.to and the personal
                # LinkedIn both come back as the same person's name. Only
                # providerIdentifier tells the two platforms apart.
                "platform": integration.get("providerIdentifier"),
                "channel": channel,
                "text": text[:_SOCIAL_TIMELINE_TEXT],
                "url": post.get("releaseURL"),
            }
        )
    rows.sort(key=lambda r: r["date"], reverse=True)
    now_str = now.strftime("%Y-%m-%d %H:%M")

    # Complete per-channel roll-up over EVERY row, built before the byte budget
    # drops any of them. Channel coverage must not depend on which sample rows
    # happened to survive truncation.
    grouped: dict[tuple[str, str], dict] = {}
    for row in rows:
        entry = grouped.setdefault(
            (row["platform"] or "unknown", row["channel"] or "unknown"),
            {"name": row["channel"], "posts": 0, "queued": 0, "published": 0, "next": None},
        )
        entry["posts"] += 1
        upper = row["state"].upper()
        if upper == "QUEUE":
            entry["queued"] += 1
        elif upper == "PUBLISHED":
            entry["published"] += 1
        if row["date"] >= now_str and (entry["next"] is None or row["date"] < entry["next"]):
            entry["next"] = row["date"]
    platforms = [platform for platform, _ in grouped]
    # Busiest channels first, so the ones that survive the byte cap are the ones
    # the question is most likely about. Once one entry overflows, every later
    # (smaller) one joins it — otherwise the kept set would cherry-pick by name
    # length rather than being an honest "top N by post count".
    channels: dict[str, dict] = {}
    overflow: list[dict] = []
    summary_used = 0
    for (platform, name), entry in sorted(grouped.items(), key=lambda kv: (-kv[1]["posts"], kv[0])):
        key = platform if platforms.count(platform) == 1 else f"{platform} ({name})"
        size = len(json.dumps({key: entry}, default=str))
        if overflow or summary_used + size > _SOCIAL_TIMELINE_CHANNEL_CAP:
            overflow.append(entry)
            continue
        channels[key] = entry
        summary_used += size
    if overflow:
        channels[f"+{len(overflow)} more"] = {
            "channels": len(overflow),
            "posts": sum(e["posts"] for e in overflow),
            "queued": sum(e["queued"] for e in overflow),
            "published": sum(e["published"] for e in overflow),
        }
    channels_json = json.dumps(channels, default=str)

    # Sample nearest-to-now first — alternating soonest-upcoming with
    # most-recent-past — so a truncated timeline straddles both sides of today
    # instead of showing only the far future. Display order stays newest-first.
    future = [r for r in rows if r["date"] >= now_str][::-1]
    past = [r for r in rows if r["date"] < now_str]
    budget = _SOCIAL_TIMELINE_BUDGET - max(
        0, len(channels_json) - _SOCIAL_TIMELINE_SUMMARY_ALLOWANCE
    )

    kept: list[dict] = []
    used = 0
    for row in [r for pair in zip_longest(future, past) for r in pair if r is not None]:
        size = len(json.dumps(row, default=str))
        if kept and used + size > budget:
            break
        kept.append(row)
        used += size
    kept.sort(key=lambda r: r["date"], reverse=True)

    return json.dumps(
        {
            # `posts` first: if this ever does overflow, the key-order truncator
            # keeps the leading keys, so the data survives and metadata is what
            # gets dropped — the opposite of the failure this budget prevents.
            # `channels_in_window` is second for the same reason: it is the
            # complete channel answer and must outrank the metadata.
            "posts": kept,
            "channels_in_window": channels,
            "count": len(kept),
            "total_in_window": len(rows),
            "truncated": len(kept) < len(rows),
            "window": {"days_back": days_back, "days_ahead": days_ahead, "state": state or None},
        },
        default=str,
    )


# `social_accounts` is 6 rows in prod, but the roll-up has to stay inside
# `_truncate_result`'s 4096-byte cap for the same reason `social_timeline` does:
# over budget, `_smart_subset` keeps the first N KEYS of the dict and drops the
# `channels` list wholesale, so the model gets metadata and no answer.
_SOCIAL_CHANNELS_BUDGET = 2800
_SOCIAL_CHANNELS_NAME = 60


@aegis_tool
async def _exec_list_social_channels(pool: asyncpg.Pool, ctx: ToolContext) -> str:
    """The social channels AEGIS is actually connected to and can publish to — the `social_accounts` mirror, which is what the publishing pipeline resolves a post against. Use this for ANY question about which channels exist, are connected, or can be posted to, and to check whether a specific platform (Bluesky, LinkedIn, Medium, X…) is set up. Do NOT answer that from `social_timeline`: that tool reports POSTS in a time window, so a connected channel with nothing scheduled is invisible to it and reads as 'not configured'. `todoist_label` is the label to put on a @publish task to route it to that channel; `labeled_but_not_connected` lists platforms that have such a label but NO account, so posts labelled for them cannot go out until the channel is connected.

    Returns:
        Straight from `social_accounts`. The gap this closes (#184): nothing was
        backed by `social_accounts`, so
    "which social channels can you post to?" had to be inferred from
    `social_timeline` — a view of POSTS, not of channels. On 2026-08-01 that
    inference reported a connected, mirrored Bluesky channel with 5 queued
    posts as absent, because the sampled window happened not to include it.

    `labeled_but_not_connected` is the other half of the answer: platforms the
    label map routes to with no account behind them, which is exactly the state
    that silently swallows a @publish task.
    """
    rows = await pool.fetch(
        "SELECT platform, label, meta, expires_at FROM social_accounts "
        "ORDER BY platform, label"
    )
    label_map = await pool.fetchval(
        "SELECT value FROM settings WHERE key = 'social_platform_labels'"
    )
    label_map = label_map if isinstance(label_map, dict) else {}
    enabled = await pool.fetchval(
        "SELECT value FROM settings WHERE key = 'social_publishing_enabled'"
    )

    channels: list[dict] = []
    used = 0
    for r in rows:
        meta = r["meta"] or {}
        entry = {
            "platform": r["platform"],
            "channel": str(r["label"] or "")[:_SOCIAL_CHANNELS_NAME],
            # Postiz-mirrored rows hold no tokens of their own; native ones do.
            "via": meta.get("via") or ("postiz" if meta.get("postiz_integration_id") else "native"),
            "todoist_label": label_map.get(r["platform"]),
        }
        size = len(json.dumps(entry, default=str))
        if channels and used + size > _SOCIAL_CHANNELS_BUDGET:
            break
        channels.append(entry)
        used += size

    connected = {r["platform"] for r in rows}
    return json.dumps(
        {
            # `channels` leads so any future overflow sheds metadata, not the answer.
            "channels": channels,
            "count": len(channels),
            "total": len(rows),
            "truncated": len(channels) < len(rows),
            "labeled_but_not_connected": {
                platform: label
                for platform, label in sorted(label_map.items())
                if platform not in connected
            },
            "publishing_enabled": bool(enabled),
        },
        default=str,
    )
