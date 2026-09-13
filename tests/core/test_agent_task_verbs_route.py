"""`agent_task_verbs` — source tag → the agent-task lane's verb (#344, #558).

Lenient `merge` (what the worker reads), strict `validate` (what the admin PUT
accepts), and the GET/PUT pair on the Todoist page.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.db import run_migrations
from aegis.services.agent_task_verbs import (
    DEFAULT_VERBS,
    UNTAGGED,
    VERBS,
    merge,
    overrides_of,
    validate,
)
from httpx import ASGITransport, AsyncClient

_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
}
AUTH = ("admin", "admin")
URL = "/api/admin/todoist/agent-task-verbs"


# ── merge / validate ──


def test_merge_ignores_an_unknown_verb_and_honours_none():
    merged = merge({"#chat": "coding", "#calendar": None, "#research": "ask"})
    assert merged["#chat"] == DEFAULT_VERBS["#chat"]  # typo'd verb ignored
    assert merged["#calendar"] is None
    assert merged["#research"] == "ask"


@pytest.mark.parametrize("value", [None, "x", [], 3])
def test_merge_of_a_non_object_is_the_defaults(value):
    assert merge(value) == DEFAULT_VERBS


def test_overrides_of_drops_what_the_read_would_ignore():
    assert overrides_of({"#chat": "nope", "#calendar": None}) == {"#calendar": None}
    assert overrides_of("x") == {}


@pytest.mark.parametrize(
    ("raw", "needle"),
    [
        ("x", "object"),
        ({"calendar": "ask"}, "not a source tag"),
        ({"#two words": "ask"}, "not a source tag"),
        ({"": "ask"}, "not a source tag"),
        ({"#chat": "coding"}, "not a verb"),
        ({"#chat": "Ask"}, "not a verb"),
        ({"#chat": "ask", " #chat ": "research"}, "duplicate"),
    ],
)
def test_validate_refuses(raw, needle):
    with pytest.raises(ValueError, match=needle):
        validate(raw)


def test_validate_accepts_every_verb_none_untagged_and_a_new_tag():
    raw = {**{f"#t{i}": v for i, v in enumerate(sorted(VERBS))}, UNTAGGED: None, " #new ": "ask"}
    out = validate(raw)
    assert out[UNTAGGED] is None and out["#new"] == "ask"
    assert validate(None) == {}


# ── the admin route ──


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM settings WHERE key = 'agent_task_verbs'")
    await db_pool.execute("DELETE FROM audit_log WHERE action = 'agent_task_verbs_saved'")
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = 'agent_task_verbs'")
    await db_pool.execute("DELETE FROM audit_log WHERE action = 'agent_task_verbs_saved'")


@pytest_asyncio.fixture(loop_scope="function")
async def client(pool):
    app = create_app(run_lifespan=False)
    app.state.db_pool = pool
    app.state.llm = AsyncMock()
    app.dependency_overrides[get_settings] = lambda: Settings(**_SETTINGS)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_requires_auth(client):
    assert (await client.get(URL)).status_code == 401


async def test_get_shows_the_defaults_and_the_vocabulary(client):
    r = await client.get(URL, auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["overrides"] == {}
    assert body["effective"] == DEFAULT_VERBS == body["defaults"]
    assert body["verbs"] == sorted(VERBS)
    assert body["untagged"] == UNTAGGED


async def test_put_changes_the_effective_table_and_audits(client, pool):
    r = await client.put(
        URL, auth=AUTH, json={"overrides": {"#calendar": None, UNTAGGED: "research"}}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["overrides"] == {"#calendar": None, UNTAGGED: "research"}
    assert body["effective"]["#calendar"] is None
    assert body["effective"][UNTAGGED] == "research"
    assert body["effective"]["#alert"] == DEFAULT_VERBS["#alert"]
    audit = await pool.fetchrow(
        "SELECT actor, details FROM audit_log WHERE action = 'agent_task_verbs_saved'"
    )
    assert audit["actor"] == "admin"
    assert audit["details"] == {"overrides": {"#calendar": None, UNTAGGED: "research"}}


async def test_put_empty_deletes_the_row(client, pool):
    await client.put(URL, auth=AUTH, json={"overrides": {"#chat": "research"}})
    r = await client.put(URL, auth=AUTH, json={"overrides": {}})
    assert r.status_code == 200
    assert r.json()["effective"] == DEFAULT_VERBS
    assert await pool.fetchval("SELECT count(*) FROM settings WHERE key = 'agent_task_verbs'") == 0


async def test_put_400s_on_a_bad_verb_and_writes_nothing(client, pool):
    r = await client.put(URL, auth=AUTH, json={"overrides": {"#chat": "coding"}})
    assert r.status_code == 400
    assert "not a verb" in r.json()["detail"]
    assert await pool.fetchval("SELECT count(*) FROM settings WHERE key = 'agent_task_verbs'") == 0
