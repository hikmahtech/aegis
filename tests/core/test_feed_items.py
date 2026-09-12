"""The recent feed items view: the newest RSS entries across the feeds.

`services/feeds.py::recent_items` and `GET /api/admin/channels/feed-items`.
Real Postgres throughout — the view is one SQL query over `feed_entries`,
`knowledge_content`, `knowledge_chunks` and `knowledge_injection_log`.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from aegis.api.auth import verify_auth
from aegis.api.routes import channels
from aegis.services import feeds
from fastapi import FastAPI

_PREFIX = "https://zzfeeditems.test/"
_THREAD = "zzfeeditems-thread"


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    async def clean():
        await db_pool.execute("DELETE FROM knowledge_injection_log WHERE thread_id = $1", _THREAD)
        # feed_entries go with their channel (ON DELETE CASCADE).
        await db_pool.execute("DELETE FROM channels WHERE identifier LIKE $1", _PREFIX + "%")
        await db_pool.execute("DELETE FROM knowledge_chunks WHERE content_id LIKE 'zzfeeditems-%'")
        await db_pool.execute("DELETE FROM knowledge_content WHERE content_id LIKE 'zzfeeditems-%'")

    await clean()
    yield db_pool
    await clean()


@pytest_asyncio.fixture(loop_scope="function")
async def client(pool):
    app = FastAPI()
    app.include_router(channels.router)
    app.dependency_overrides[verify_auth] = lambda: True
    app.state.db_pool = pool
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def _channel(pool, name: str, label: str | None = None) -> str:
    return await pool.fetchval(
        "INSERT INTO channels (kind, identifier, config) VALUES ('rss', $1, $2) RETURNING id::text",
        _PREFIX + name,
        {"label": label or name},
    )


async def _item(
    pool,
    cid: str,
    n: int,
    *,
    mode: str = "full",
    minutes_ago: int = 0,
    title: str | None = None,
    summary: str | None = None,
    chunk: str | None = None,
    link: str | None = None,
) -> str | None:
    """One feed entry and, unless it failed, the knowledge row it produced."""
    content_id = None if mode == "failed" else f"zzfeeditems-{cid[:8]}-{n}"
    link = link if link is not None else f"https://zzfeeditems.test/{cid[:8]}/{n}"
    if content_id:
        await pool.execute(
            "INSERT INTO knowledge_content (content_id, url, title, source_type, summary) "
            "VALUES ($1, $2, $3, 'rss', $4)",
            content_id,
            link,
            title if title is not None else f"Item {n}",
            summary,
        )
        if chunk:
            await pool.execute(
                "INSERT INTO knowledge_chunks (content_id, chunk_index, chunk_text) VALUES ($1, 0, $2)",
                content_id,
                chunk,
            )
    await pool.execute(
        "INSERT INTO feed_entries (channel_id, external_id, link, content_id, mode, seen_at) "
        "VALUES ($1::uuid, $2, $3, $4, $5, now() - make_interval(mins => $6))",
        cid,
        f"zzext-{cid[:8]}-{n}",
        link,
        content_id,
        mode,
        minutes_ago,
    )
    return content_id


def _titles(result: dict) -> list[str]:
    return [i["title"] for i in result["items"]]


async def test_items_come_newest_first_across_feeds_with_their_feed_and_storage(pool):
    a = await _channel(pool, "a", "Feed A")
    b = await _channel(pool, "b", "Feed B")
    await _item(pool, a, 1, minutes_ago=30, title="Old A")
    await _item(pool, b, 2, minutes_ago=10, title="Newer B", mode="abstract", summary="B's summary.")
    await _item(pool, a, 3, minutes_ago=1, title="Newest A")

    got = await feeds.recent_items(pool, limit=200)
    mine = [i for i in got["items"] if i["feed_url"].startswith(_PREFIX)]
    assert [i["title"] for i in mine] == ["Newest A", "Newer B", "Old A"]
    newer_b = mine[1]
    assert newer_b["feed"] == "Feed B"
    assert newer_b["mode"] == "abstract"
    assert newer_b["excerpt"] == "B's summary."
    assert newer_b["link"].startswith("https://zzfeeditems.test/")
    assert newer_b["used"] is False


async def test_one_feed_and_one_storage_mode(pool):
    a = await _channel(pool, "a")
    b = await _channel(pool, "b")
    await _item(pool, a, 1, minutes_ago=3)
    await _item(pool, a, 2, minutes_ago=2, mode="failed")
    await _item(pool, b, 3, minutes_ago=1)

    only_a = await feeds.recent_items(pool, channel_id=a)
    assert {i["channel_id"] for i in only_a["items"]} == {a}
    assert len(only_a["items"]) == 2

    failed = await feeds.recent_items(pool, channel_id=a, mode="failed")
    assert [i["mode"] for i in failed["items"]] == ["failed"]
    # A failed entry has no knowledge row: its title falls back to its link.
    assert failed["items"][0]["title"].startswith("https://zzfeeditems.test/")
    assert failed["items"][0]["excerpt"] == ""


async def test_the_excerpt_is_the_start_of_the_stored_page_without_its_title(pool):
    a = await _channel(pool, "a")
    await _item(
        pool,
        a,
        1,
        title="A long read",
        chunk="A long read\n\nThe   body starts here.  " + "More text. " * 60,
    )
    item = (await feeds.recent_items(pool, channel_id=a))["items"][0]
    assert item["excerpt"].startswith("The body starts here.")
    assert len(item["excerpt"]) <= 281 and item["excerpt"].endswith("…")


async def test_used_means_a_prompt_put_the_document_in(pool):
    a = await _channel(pool, "a")
    used_id = await _item(pool, a, 1, minutes_ago=2, title="Used one")
    await _item(pool, a, 2, minutes_ago=1, title="Unused one")
    await pool.execute(
        "INSERT INTO knowledge_injection_log (agent_id, thread_id, source, content_ids) "
        "VALUES ('raphael', $1, 'research', $2)",
        _THREAD,
        [used_id],
    )
    by_title = {i["title"]: i for i in (await feeds.recent_items(pool, channel_id=a))["items"]}
    assert by_title["Used one"]["used"] is True
    assert by_title["Unused one"]["used"] is False


async def test_pages_never_repeat_or_skip_an_item(pool):
    a = await _channel(pool, "a")
    for n in range(5):
        await _item(pool, a, n, minutes_ago=10 - n, title=f"Item {n}")

    first = await feeds.recent_items(pool, channel_id=a, limit=2)
    assert _titles(first) == ["Item 4", "Item 3"]
    assert first["next_cursor"]
    # An entry arriving between pages does not shift the next one.
    await _item(pool, a, 9, minutes_ago=0, title="Arrived late")
    second = await feeds.recent_items(pool, channel_id=a, limit=2, cursor=first["next_cursor"])
    assert _titles(second) == ["Item 2", "Item 1"]
    last = await feeds.recent_items(pool, channel_id=a, limit=2, cursor=second["next_cursor"])
    assert _titles(last) == ["Item 0"]
    assert last["next_cursor"] is None


async def test_a_link_that_is_not_a_web_address_is_never_returned(pool):
    a = await _channel(pool, "a")
    await _item(pool, a, 1, title="Sneaky", link="javascript:alert(1)")
    item = (await feeds.recent_items(pool, channel_id=a))["items"][0]
    assert item["link"] == ""
    assert item["title"] == "Sneaky"


async def test_bad_mode_and_bad_cursor_are_refused(pool):
    with pytest.raises(ValueError):
        await feeds.recent_items(pool, mode="everything")
    with pytest.raises(ValueError):
        await feeds.recent_items(pool, cursor="not-a-cursor")


async def test_the_route_returns_a_page_and_validates_its_input(client, pool):
    a = await _channel(pool, "a", "Route feed")
    await _item(pool, a, 1, title="Via the route")

    ok = await client.get(f"/api/admin/channels/feed-items?channel_id={a}&limit=10")
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert _titles(body) == ["Via the route"]
    assert body["items"][0]["feed"] == "Route feed"
    assert body["next_cursor"] is None

    assert (await client.get("/api/admin/channels/feed-items?mode=everything")).status_code == 422
    assert (await client.get("/api/admin/channels/feed-items?channel_id=nope")).status_code == 422
    assert (await client.get("/api/admin/channels/feed-items?cursor=garbage")).status_code == 400
