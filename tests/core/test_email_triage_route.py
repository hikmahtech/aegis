"""Tests for /api/admin/email/triage-rules.

The point of this endpoint over the generic /api/settings editor is that it
validates. `merge` is deliberately lenient so a malformed row can never stop
mail being classified — which means the generic editor accepts a typo'd category
and the rule then vanishes with no error anywhere. These tests exist to keep the
write path loud.
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
URL = "/api/admin/email/triage-rules"


@pytest.fixture
def settings():
    return Settings(**_SETTINGS)


@pytest_asyncio.fixture(loop_scope="function")
async def rules_pool(db_pool):
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM settings WHERE key='email_triage_rules'")
        await conn.execute("DELETE FROM triage_state WHERE email_addr LIKE 'et-%'")
        await conn.execute(
            "INSERT INTO triage_state (email_addr, state, metadata) VALUES "
            "('et-stuck@example.com','important_action','{\"n\":5,\"confidence\":0.9}'::jsonb),"
            "('et-fresh@example.com','important_read','{\"n\":1,\"confidence\":0.6}'::jsonb)"
        )
    yield db_pool
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM settings WHERE key='email_triage_rules'")
        await conn.execute("DELETE FROM triage_state WHERE email_addr LIKE 'et-%'")


@pytest_asyncio.fixture(loop_scope="function")
async def app_client(settings, rules_pool):
    app = create_app(run_lifespan=False)
    app.state.db_pool = rules_pool
    app.state.llm = AsyncMock()
    app.dependency_overrides[get_settings] = lambda: settings
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, app


async def test_requires_auth(app_client):
    client, _ = app_client
    assert (await client.get(URL)).status_code == 401


async def test_get_returns_empty_defaults_and_the_category_vocabulary(app_client):
    """A fork must arrive with no rules but a usable page — the category list is
    what the UI builds its <select> from, so an empty one silently gives you a
    dropdown with no options."""
    client, _ = app_client
    r = await client.get(URL, auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["sender_overrides"] == {}
    assert body["extra_notification_markers"] == []
    assert set(body["categories"]) == {
        "important_action", "important_read", "informational", "useless",
    }


async def test_round_trip_persists_and_normalises(app_client):
    client, _ = app_client
    r = await client.put(
        URL,
        auth=AUTH,
        json={
            "sender_overrides": {"  News@Substack.COM ": "informational"},
            "extra_notification_markers": ["  Incorrect Login Attempt  ", "   "],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["sender_overrides"] == {
        "news@substack.com": {"category": "informational", "tags": []}
    }
    assert r.json()["extra_notification_markers"] == ["incorrect login attempt"]
    # survives a fresh read
    assert (await client.get(URL, auth=AUTH)).json()["sender_overrides"] == {
        "news@substack.com": {"category": "informational", "tags": []}
    }


async def test_tagged_rule_round_trips(app_client):
    """(#263) The tags are what keep the money fan-out alive for an overridden
    biller, so they have to survive the write path, not just `merge`."""
    client, _ = app_client
    r = await client.put(
        URL, auth=AUTH,
        json={"sender_overrides": {"Bank@Example.com": {
            "category": "important_read", "tags": ["Financial", "receipt"],
        }}},
    )
    assert r.status_code == 200, r.text
    assert (await client.get(URL, auth=AUTH)).json()["sender_overrides"] == {
        "bank@example.com": {"category": "important_read", "tags": ["financial", "receipt"]}
    }


async def test_malformed_tags_are_a_400_not_a_silent_drop(app_client):
    """Same contract as a typo'd category: `merge` drops junk tags so mail keeps
    being classified, which means the write path is the only place the author
    can be told their receipt fan-out will never fire."""
    client, _ = app_client
    for bad in ("financial", ["ok", 7], ["  "], [None]):
        r = await client.put(
            URL, auth=AUTH,
            json={"sender_overrides": {"a@b.com": {"category": "useless", "tags": bad}}},
        )
        assert r.status_code == 400, (bad, r.text)
    assert (await client.get(URL, auth=AUTH)).json()["sender_overrides"] == {}


async def test_invalid_category_is_a_400_not_a_silent_drop(app_client):
    """The whole reason this endpoint exists. `merge` drops an unknown category
    so the classifier can't be broken by bad data; without this check the user
    would save a typo, see a 200, and never learn the rule does nothing."""
    client, _ = app_client
    r = await client.put(
        URL, auth=AUTH,
        json={"sender_overrides": {"a@b.com": "informationnal"}},
    )
    assert r.status_code == 400
    assert "informationnal" in r.json()["detail"]
    # and nothing was written
    assert (await client.get(URL, auth=AUTH)).json()["sender_overrides"] == {}


async def test_malformed_shapes_are_rejected(app_client):
    client, _ = app_client
    for bad in (
        {"sender_overrides": ["a@b.com"]},
        {"sender_overrides": {"": "useless"}},
        {"sender_overrides": {"a@b.com": {"category": "informationnal"}}},
        {"sender_overrides": {"a@b.com": {"tags": ["financial"]}}},
        {"extra_notification_markers": "not a list"},
        {"extra_notification_markers": ["ok", 123]},
    ):
        assert (await client.put(URL, auth=AUTH, json=bad)).status_code == 400, bad


async def test_blank_marker_row_is_dropped_not_rejected(app_client):
    """A leftover empty row in the editor is not a mistake that changes meaning
    — unlike a typo'd category, it can't silently do the wrong thing. Rejecting
    it would just make the page fail to save for no reason."""
    client, _ = app_client
    r = await client.put(
        URL, auth=AUTH,
        json={"extra_notification_markers": ["account blocked", "   "]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["extra_notification_markers"] == ["account blocked"]


async def test_known_senders_flags_the_ones_that_skip_the_llm(app_client):
    """A sender at n>=3 and confidence>=0.75 short-circuits the classifier, so a
    wrong cached verdict there can never self-correct — the page surfaces
    exactly those. Fails if the threshold drifts out of sync with
    `_CACHE_MIN_N`/`_CACHE_MIN_CONF` in the worker's classifier."""
    client, _ = app_client
    by_addr = {s["email_addr"]: s for s in (await client.get(URL, auth=AUTH)).json()["known_senders"]}
    assert by_addr["et-stuck@example.com"]["skips_llm"] is True
    assert by_addr["et-fresh@example.com"]["skips_llm"] is False
