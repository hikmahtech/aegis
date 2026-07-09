"""Social connect/callback OAuth routes (X, PKCE) — tokens land in social_accounts."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio
import respx
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.crypto import decrypt_secret

_TEST_REQUIRED_SETTINGS: dict = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "n8n_ui_url": "https://n8n.example.com",
    "admin_username": "a",
    "admin_password": "p",
    "api_key": "k",
    "n8n_webhook_secret": "test-secret",
}


@pytest.fixture
def settings():
    return Settings(
        **_TEST_REQUIRED_SETTINGS,
        # aegis_ui_url only populates via its validation_alias
        **{"AEGIS_UI_URL": "https://aegis.example.com"},
        x_client_id="x-cid",
        x_client_secret="x-cs",
        secret_key="test-secret-key",
    )


@pytest_asyncio.fixture(loop_scope="function")
async def client(settings, db_pool):
    from aegis.api.app import create_app

    app = create_app(run_lifespan=False)
    app.state.db_pool = db_pool
    app.dependency_overrides[get_settings] = lambda: settings
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM social_outbox")
        await conn.execute("DELETE FROM social_accounts")


async def test_connect_redirects_with_pkce(client):
    resp = await client.get(
        "/api/admin/social/x/connect?label=work", headers={"X-API-Key": "k"}
    )
    assert resp.status_code == 302
    loc = urlparse(resp.headers["location"])
    assert loc.hostname == "x.com"
    q = parse_qs(loc.query)
    assert q["client_id"] == ["x-cid"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["code_challenge"]
    assert q["state"]
    assert "offline.access" in q["scope"][0]
    assert q["redirect_uri"] == ["https://aegis.example.com/api/admin/social/x/callback"]


async def test_connect_unknown_platform_404(client):
    resp = await client.get("/api/admin/social/mastodon/connect", headers={"X-API-Key": "k"})
    assert resp.status_code == 404


async def test_connect_without_client_config_503(client, settings):
    settings.x_client_id = ""
    resp = await client.get("/api/admin/social/x/connect", headers={"X-API-Key": "k"})
    assert resp.status_code == 503
    settings.x_client_id = "x-cid"


@respx.mock
async def test_callback_exchanges_and_upserts_account(client, settings, db_pool):
    # Initiate first so a PKCE state exists.
    initiate = await client.get(
        "/api/admin/social/x/connect?label=work", headers={"X-API-Key": "k"}
    )
    state = parse_qs(urlparse(initiate.headers["location"]).query)["state"][0]

    token_route = respx.post("https://api.x.com/2/oauth2/token").respond(
        200,
        json={
            "access_token": "acc-1",
            "refresh_token": "ref-1",
            "expires_in": 7200,
            "scope": "tweet.read tweet.write users.read offline.access",
        },
    )
    resp = await client.get(
        f"/api/admin/social/x/callback?code=the-code&state={state}",
        headers={"X-API-Key": "k"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert token_route.called
    sent = dict(
        pair.split("=") for pair in token_route.calls[0].request.content.decode().split("&")
    )
    assert sent["grant_type"] == "authorization_code"
    assert sent["code_verifier"]

    row = await db_pool.fetchrow(
        "SELECT * FROM social_accounts WHERE platform = 'x' AND label = 'work'"
    )
    assert row is not None
    assert decrypt_secret(row["access_token_enc"], settings.secret_key) == "acc-1"
    assert decrypt_secret(row["refresh_token_enc"], settings.secret_key) == "ref-1"
    assert row["expires_at"] is not None

    # Accounts listing never returns token values.
    accounts = (
        await client.get("/api/admin/social/accounts", headers={"X-API-Key": "k"})
    ).json()
    assert [(a["platform"], a["label"]) for a in accounts] == [("x", "work")]
    assert "acc-1" not in str(accounts)
    assert "ref-1" not in str(accounts)


async def test_callback_unknown_state_400(client):
    resp = await client.get(
        "/api/admin/social/x/callback?code=c&state=bogus", headers={"X-API-Key": "k"}
    )
    assert resp.status_code == 400


# --------------------------------------------------------------- postiz sync


async def test_sync_postiz_without_config_503(client):
    resp = await client.post("/api/admin/social/postiz/sync", headers={"X-API-Key": "k"})
    assert resp.status_code == 503
    assert resp.json()["detail"] == "postiz_not_configured"


@respx.mock
async def test_sync_postiz_upserts_non_disabled_and_is_idempotent(client, settings, db_pool):
    settings.postiz_url = "https://postiz.example.com"
    settings.postiz_api_key = "pz-key"

    integrations_route = respx.get(
        "https://postiz.example.com/api/public/v1/integrations"
    ).respond(
        200,
        json=[
            {
                "id": "int-1",
                "name": "My Mastodon",
                "identifier": "mastodon",
                "picture": "https://example.com/pic.png",
                "disabled": False,
                "profile": "me@mastodon.social",
            },
            {
                "id": "int-2",
                "name": "Disabled Channel",
                "identifier": "bluesky",
                "picture": "",
                "disabled": True,
                "profile": "",
            },
        ],
    )

    resp = await client.post("/api/admin/social/postiz/sync", headers={"X-API-Key": "k"})
    assert resp.status_code == 200
    assert resp.json() == {"synced": 1, "skipped_disabled": 1}
    assert integrations_route.calls[0].request.headers["authorization"] == "pz-key"

    row = await db_pool.fetchrow(
        "SELECT platform, label, meta, access_token_enc, refresh_token_enc "
        "FROM social_accounts WHERE platform = 'mastodon'"
    )
    assert row is not None
    assert row["label"] == "my-mastodon"
    assert row["meta"]["postiz_integration_id"] == "int-1"
    assert row["meta"]["via"] == "postiz"
    assert row["access_token_enc"] is None
    assert row["refresh_token_enc"] is None

    accounts = (
        await client.get("/api/admin/social/accounts", headers={"X-API-Key": "k"})
    ).json()
    assert len(accounts) == 1
    assert accounts[0]["via"] == "postiz"

    # Re-running the sync upserts in place — no duplicate row.
    resp2 = await client.post("/api/admin/social/postiz/sync", headers={"X-API-Key": "k"})
    assert resp2.status_code == 200
    assert resp2.json() == {"synced": 1, "skipped_disabled": 1}
    count = await db_pool.fetchval("SELECT count(*) FROM social_accounts")
    assert count == 1


@respx.mock
async def test_sync_postiz_upstream_error_502(client, settings):
    settings.postiz_url = "https://postiz.example.com"
    settings.postiz_api_key = "pz-key"
    respx.get("https://postiz.example.com/api/public/v1/integrations").respond(
        500, json={"error": "boom"}
    )
    resp = await client.post("/api/admin/social/postiz/sync", headers={"X-API-Key": "k"})
    assert resp.status_code == 502


# --------------------------------------------------------------- recent posts


async def test_list_posts_returns_seeded_row_shape(client, db_pool):
    account_id = await db_pool.fetchval(
        "INSERT INTO social_accounts (platform, label, meta) VALUES ($1, $2, $3) RETURNING id",
        "mastodon",
        "posts-test",
        {"postiz_integration_id": "int-1", "via": "postiz"},
    )
    await db_pool.execute(
        "INSERT INTO social_outbox "
        "(todoist_task_id, account_id, payload, status, posted_ref, metrics, metrics_at) "
        "VALUES ($1, $2, $3, 'posted', 'pz-1', $4, now())",
        "soctest-postslist",
        account_id,
        {"text": "hello world " * 20, "link": "https://example.com", "schedule_at": "2026-07-01T09:00:00+00:00"},
        {"state": "PUBLISHED", "release_url": "https://mastodon.social/@me/1", "series": {"likes": 5}},
    )
    resp = await client.get("/api/admin/social/posts?days=14", headers={"X-API-Key": "k"})
    assert resp.status_code == 200
    rows = resp.json()
    row = next(r for r in rows if r["posted_ref"] == "pz-1")
    assert row["platform"] == "mastodon"
    assert row["label"] == "posts-test"
    assert row["status"] == "posted"
    assert row["state"] == "PUBLISHED"
    assert row["release_url"] == "https://mastodon.social/@me/1"
    assert row["series"] == {"likes": 5}
    assert row["schedule_at"] == "2026-07-01T09:00:00+00:00"
    assert len(row["text"]) <= 120
    assert row["metrics_at"] is not None
    assert row["created_at"] is not None
