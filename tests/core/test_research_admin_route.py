"""GET/PUT /api/admin/research/* — the validating write path for the research
lane's five config rows (the generic /api/settings editor validates nothing),
and the tracked-topic registry written whole under the registry lock."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.services import (
    feeds_config,
    library_config,
    research_config,
    research_topics,
    topics_config,
)
from httpx import ASGITransport, AsyncClient

from tests.core.test_research_topics import todoist, world  # noqa: F401 — fixtures

_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
}
AUTH = ("admin", "admin")
BASE = "/api/admin/research"
ROWS = (feeds_config.ROW, research_config.ROW, library_config.ROW, topics_config.ROW)


@pytest.fixture
def settings():
    return Settings(**_SETTINGS)


@pytest_asyncio.fixture(loop_scope="function")
async def pool(world):  # noqa: F811 — the topics fixture (Inbox, clean registry)
    keys = [r.key for r in ROWS]
    await world.execute("DELETE FROM settings WHERE key = ANY($1::text[])", keys)
    for r in ROWS:
        r.clear_cache()
    yield world
    await world.execute("DELETE FROM settings WHERE key = ANY($1::text[])", keys)
    for r in ROWS:
        r.clear_cache()


@pytest_asyncio.fixture(loop_scope="function")
async def client(settings, pool, todoist):  # noqa: F811
    app = create_app(run_lifespan=False)
    app.state.db_pool = pool
    app.state.llm = AsyncMock()
    app.dependency_overrides[get_settings] = lambda: settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_every_route_requires_auth(client):
    for path in ("topics", "topics-config", "feeds-config", "config", "library-config"):
        assert (await client.get(f"{BASE}/{path}")).status_code == 401


@pytest.mark.parametrize(
    ("path", "defaults", "good", "bad"),
    [
        ("feeds-config", feeds_config.DEFAULTS, {"failing_after": 4}, {"failing_after": 0}),
        ("config", research_config.merge(None), {"wait_seconds": 60}, {"wait_seconds": 1}),
        ("library-config", library_config.merge(None), {"passages": 2}, {"passages": 0}),
        ("topics-config", topics_config.merge(None), {"digest_items": 3}, {"digest_items": "x"}),
    ],
)
async def test_get_returns_the_defaults_and_put_validates(client, pool, path, defaults, good, bad):
    r = await client.get(f"{BASE}/{path}", auth=AUTH)
    assert r.status_code == 200 and r.json() == defaults
    r = await client.put(f"{BASE}/{path}", auth=AUTH, json=bad)
    assert r.status_code == 400 and r.json()["detail"]
    assert (await client.get(f"{BASE}/{path}", auth=AUTH)).json() == defaults, "nothing written"
    r = await client.put(f"{BASE}/{path}", auth=AUTH, json=good)
    assert r.status_code == 200
    for k, v in good.items():
        assert r.json()[k] == v
    assert (await client.get(f"{BASE}/{path}", auth=AUTH)).json()[list(good)[0]] == list(good.values())[0]


async def test_the_topics_registry_is_read_leniently_and_written_strictly(client, pool):
    token = uuid.uuid4().hex[:6]
    # A hand-edited row: one good entry, one junk entry.
    await pool.execute(
        "INSERT INTO settings (key, value) VALUES ($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        research_topics.TOPICS_SETTING,
        {"topics": [{"name": f"Good {token}", "queries": ["g"]}, "junk", {"name": ""}]},
    )
    r = await client.get(f"{BASE}/topics", auth=AUTH)
    assert r.status_code == 200
    names = [t["name"] for t in r.json()["topics"]]
    assert names == [f"Good {token}"]
    assert r.json()["topics"][0]["effective_threshold"] == 3
    assert r.json()["priorities"] == ["high", "medium", "low"]

    r = await client.put(f"{BASE}/topics", auth=AUTH, json={"topics": [{"name": "X", "priority": "urgent"}]})
    assert r.status_code == 400 and "priority" in r.json()["detail"]
    assert [t["name"] for t in (await client.get(f"{BASE}/topics", auth=AUTH)).json()["topics"]] == names

    r = await client.put(
        f"{BASE}/topics",
        auth=AUTH,
        json={
            "topics": [
                {"name": f"Good {token}", "queries": ["g", "h"], "priority": "high"},
                {"name": f"New {token}", "queries": ["n"], "priority": "low", "threshold": 1},
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = {t["name"]: t for t in r.json()["topics"]}
    assert body[f"Good {token}"]["queries"] == ["g", "h"]
    assert body[f"New {token}"]["effective_threshold"] == 1
    # The new topic got its round opened, as `track` would give it.
    assert body[f"New {token}"]["round"]["items"] == 0
    assert body[f"New {token}"]["round"]["task_id"] is None

    # Dropping a topic closes its round.
    pid = body[f"New {token}"]["round"]["problem_id"]
    r = await client.put(
        f"{BASE}/topics", auth=AUTH, json={"topics": [{"name": f"Good {token}", "queries": ["g"]}]}
    )
    assert r.status_code == 200
    assert await pool.fetchval("SELECT closed_at FROM problems WHERE id = $1::uuid", pid) is not None
    await research_topics.untrack(pool, f"Good {token}")


async def test_an_admin_save_and_a_chat_track_landing_together_stay_consistent(pool):
    """Both writers take the registry's advisory lock, so the two
    read-modify-writes are serialised: the registry ends as one of the two
    orders would leave it, never torn, and the hub's rounds agree with it —
    a topic in the registry has a live round, a topic dropped by the save
    has its round closed."""
    token = uuid.uuid4().hex[:6]
    base, admin, chat = f"Base {token}", f"Admin {token}", f"Chat {token}"
    await research_topics.track(pool, base, ["b"])
    await asyncio.gather(
        research_topics.save_registry(
            pool, {"topics": [{"name": base, "queries": ["b"]}, {"name": admin, "queries": ["a"]}]}
        ),
        research_topics.track(pool, chat, ["c"]),
    )
    topics = {t.name: t for t in await research_topics.load_topics(pool)}
    assert set(topics) in ({base, admin}, {base, admin, chat}), topics
    for name, topic in topics.items():
        assert await research_topics.live_problem(pool, topic) is not None, name
    if chat not in topics:
        gone = await research_topics._open_round(pool, research_topics.Topic(chat, ()))
        assert gone is None, "the save dropped the chat topic, so its round was closed"
    # Serialised after the save, a chat track always lands.
    await research_topics.track(pool, chat, ["c"])
    assert chat in {t.name for t in await research_topics.load_topics(pool)}
    for name in (base, admin, chat):
        await research_topics.untrack(pool, name)


async def test_a_topics_threshold_from_the_config_decides_when_a_round_earns_a_task(client, pool):
    """A non-default attention number changes behaviour: with `high: 1` the
    first item marks the round, with the default 2 it does not."""
    token = uuid.uuid4().hex[:6]
    await research_topics.track(pool, f"Thr {token}", [f"kw{token}"], "high")
    topic = next(t for t in await research_topics.load_topics(pool) if token in t.name)
    item = {"title": f"Story about kw{token}", "url": f"https://x.example/{token}/1", "summary": ""}
    out = await research_topics.attach_items(pool, [item], origin="test", project=False)
    assert out["tasks"] == 0
    r = await client.put(f"{BASE}/topics-config", auth=AUTH, json={"attention": {"high": 1}})
    assert r.status_code == 200
    item2 = {"title": f"Another kw{token}", "url": f"https://x.example/{token}/2", "summary": ""}
    out = await research_topics.attach_items(pool, [item2], origin="test", project=False)
    assert out["tasks"] == 1
    problem = await research_topics.live_problem(pool, topic)
    assert problem["metadata"]["attention"] is True and problem["metadata"]["attention_items"] == 2
    assert (await research_topics.track(pool, f"Thr {token}", ["k"], "high"))["task_after_items"] == 1
    assert (await research_topics.track(pool, f"Own {token}", ["k"], "low", threshold=9))[
        "task_after_items"
    ] == 9
    assert len(await research_topics.round_items(pool, problem["id"])) == 2
    await client.put(f"{BASE}/topics-config", auth=AUTH, json={"digest_items": 1})
    assert len(await research_topics.round_items(pool, problem["id"])) == 1
    # Close the rounds: the shared test database's hub sweep would otherwise
    # project this attention-marked round into a task in a later test.
    for name in (f"Thr {token}", f"Own {token}"):
        await research_topics.untrack(pool, name)
