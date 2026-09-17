"""GET/PUT /api/admin/email/task-links — the validating write path #337 asked for.

`email_task_links.merge` drops a malformed rule with a log line, so until this
pair existed a typo'd action or an unclosed regex saved through the generic
`/api/settings` editor with a 200 and then matched nothing, forever.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.db import run_migrations
from httpx import ASGITransport, AsyncClient

_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
}
AUTH = ("admin", "admin")
URL = "/api/admin/email/task-links"
RULE = {
    "key": "jira-done",
    "subject_re": r"\((APP-\d+)\)",
    "body_re": r"resolution\s*:\s*Done",
    "action": "complete",
}


@pytest.fixture
def settings():
    return Settings(**_SETTINGS)


@pytest_asyncio.fixture(loop_scope="function")
async def links_pool(db_pool):
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM settings WHERE key='email_task_links'")
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key='email_task_links'")


@pytest_asyncio.fixture(loop_scope="function")
async def app_client(settings, links_pool):
    app = create_app(run_lifespan=False)
    app.state.db_pool = links_pool
    app.state.llm = AsyncMock()
    app.dependency_overrides[get_settings] = lambda: settings
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_requires_auth(app_client):
    assert (await app_client.get(URL)).status_code == 401


async def test_get_ships_empty_with_the_action_vocabulary(app_client):
    r = await app_client.get(URL, auth=AUTH)
    assert r.status_code == 200
    assert r.json() == {"actions": ["complete", "unblock", "comment"], "links": []}


async def test_put_round_trips(app_client):
    r = await app_client.put(URL, auth=AUTH, json={"links": [RULE]})
    assert r.status_code == 200, r.text
    assert r.json()["links"] == [RULE]
    assert (await app_client.get(URL, auth=AUTH)).json()["links"] == [RULE]


@pytest.mark.parametrize(
    "bad,message",
    [
        ({**RULE, "action": "clsoe"}, "is not an action"),
        ({**RULE, "subject_re": "("}, "not a valid regular expression"),
        ({**RULE, "body_re": "(unclosed"}, "not a valid regular expression"),
        ({**RULE, "key": ""}, "key required"),
    ],
)
async def test_put_400s_on_what_the_read_path_would_drop_silently(app_client, bad, message):
    r = await app_client.put(URL, auth=AUTH, json={"links": [bad]})
    assert r.status_code == 400 and message in r.json()["detail"]


@pytest.mark.parametrize("body", [{}, {"links": None}, {"link": [RULE]}])
async def test_a_body_without_links_is_refused_not_read_as_empty(app_client, body):
    """A PUT is a REPLACEMENT, so `body.get("links") or []` would answer 200 to a
    typo'd key and wipe every rule. The rules are only removable on purpose."""
    await app_client.put(URL, auth=AUTH, json={"links": [RULE]})

    r = await app_client.put(URL, auth=AUTH, json=body)
    assert r.status_code == 400 and "links is required" in r.json()["detail"]
    assert (await app_client.get(URL, auth=AUTH)).json()["links"] == [RULE], "the rules were wiped"


async def test_an_explicit_empty_list_still_removes_every_rule(app_client):
    await app_client.put(URL, auth=AUTH, json={"links": [RULE]})
    r = await app_client.put(URL, auth=AUTH, json={"links": []})
    assert r.status_code == 200 and r.json()["links"] == []


async def test_duplicate_keys_are_refused(app_client):
    r = await app_client.put(URL, auth=AUTH, json={"links": [RULE, {**RULE, "action": "comment"}]})
    assert r.status_code == 400 and "duplicate rule key" in r.json()["detail"]
