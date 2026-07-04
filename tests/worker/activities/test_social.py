"""SocialActivities + SocialConnector — real Postgres, respx-mocked X API."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import respx
from aegis.config import Settings
from aegis.connectors.social import SocialConnector
from aegis.crypto import decrypt_secret, encrypt_secret
from aegis_worker.activities.social import SocialActivities
from httpx import Response
from temporalio.testing import ActivityEnvironment

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

_SETTINGS_KEYS = [
    "social_publishing_enabled",
    "social_publish_label",
    "social_platform_labels",
    "user_timezone",
]


def _settings() -> Settings:
    return Settings(
        **_TEST_REQUIRED_SETTINGS,
        x_client_id="x-cid",
        x_client_secret="x-cs",
        secret_key="",  # plaintext stored-secret mode
    )


def _settings_with_postiz() -> Settings:
    return Settings(
        **_TEST_REQUIRED_SETTINGS,
        x_client_id="x-cid",
        x_client_secret="x-cs",
        secret_key="",
        postiz_url="https://postiz.example.com",
        postiz_api_key="pz-key",
    )


@pytest_asyncio.fixture(loop_scope="function")
async def social_env(db_pool):
    """Seed social settings (saving originals), clean tables before/after."""
    async with db_pool.acquire() as conn:
        originals = {
            r["key"]: r["value"]
            for r in await conn.fetch(
                "SELECT key, value FROM settings WHERE key = ANY($1::text[])", _SETTINGS_KEYS
            )
        }
        for key, value in {
            "social_publishing_enabled": True,
            "social_publish_label": "publish",
            "social_platform_labels": {"x": "x", "linkedin": "linkedin"},
            "user_timezone": "UTC",
        }.items():
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES ($1, $2) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                key,
                value,
            )
        await conn.execute("DELETE FROM social_outbox")
        await conn.execute("DELETE FROM social_accounts")
        await conn.execute("DELETE FROM todoist_outbox WHERE temp_id LIKE 'social-%'")
        await conn.execute("DELETE FROM todoist_tasks WHERE id LIKE 'soctest-%'")
    yield db_pool
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM social_outbox")
        await conn.execute("DELETE FROM social_accounts")
        await conn.execute("DELETE FROM todoist_outbox WHERE temp_id LIKE 'social-%'")
        await conn.execute("DELETE FROM todoist_tasks WHERE id LIKE 'soctest-%'")
        for key in _SETTINGS_KEYS:
            if key in originals:
                await conn.execute(
                    "UPDATE settings SET value = $2 WHERE key = $1", key, originals[key]
                )
            else:
                await conn.execute("DELETE FROM settings WHERE key = $1", key)


async def _seed_task(
    pool,
    task_id: str,
    labels: list[str],
    due_datetime: str | None = None,
    due_date=None,
    content: str = "hello world",
    description: str = "",
):
    # Sync-API shape: a timed due lives in due.date ("YYYY-MM-DDTHH:MM:SS",
    # "...Z" for fixed-tz dues). due.datetime is the REST-API shape, covered
    # separately by test_find_due_posts_rest_api_datetime_fallback.
    raw = {"due": {"date": due_datetime, "timezone": None}} if due_datetime else {}
    await pool.execute(
        "INSERT INTO todoist_tasks (id, content, description, due_date, labels, raw) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        task_id,
        content,
        description,
        due_date,
        labels,
        raw,
    )


async def _seed_account(
    pool,
    platform: str = "x",
    label: str = "test",
    access: str = "old-acc",
    refresh: str = "old-ref",
    expires_in_seconds: int = 3600,
) -> int:
    return await pool.fetchval(
        "INSERT INTO social_accounts "
        "(platform, label, access_token_enc, refresh_token_enc, expires_at) "
        "VALUES ($1, $2, $3, $4, now() + make_interval(secs => $5)) RETURNING id",
        platform,
        label,
        encrypt_secret(access, ""),
        encrypt_secret(refresh, ""),
        expires_in_seconds,
    )


async def _seed_postiz_account(
    pool,
    platform: str = "mastodon",
    label: str = "postiz-test",
    integration_id: str = "int-1",
) -> int:
    """Postiz-mirrored account: no tokens of its own, NULL access/refresh/expires_at."""
    return await pool.fetchval(
        "INSERT INTO social_accounts (platform, label, meta) VALUES ($1, $2, $3) RETURNING id",
        platform,
        label,
        {"postiz_integration_id": integration_id, "via": "postiz"},
    )


class _FakeConnector:
    def __init__(self, ref: str = "ref-1", fail: bool = False):
        self.ref = ref
        self.fail = fail
        self.calls: list[tuple[int, dict]] = []

    async def post(self, account_id: int, payload: dict) -> str:
        self.calls.append((account_id, payload))
        if self.fail:
            raise RuntimeError("boom")
        return self.ref


# ---------------------------------------------------------------- find_due_posts


async def test_find_due_posts_disabled_returns_empty(social_env):
    await social_env.execute(
        "UPDATE settings SET value = 'false'::jsonb WHERE key = 'social_publishing_enabled'"
    )
    await _seed_task(
        social_env,
        "soctest-1",
        ["publish", "x"],
        due_datetime=(datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    act = SocialActivities(db_pool=social_env)
    assert await ActivityEnvironment().run(act.find_due_posts, 10, 9) == []


async def test_find_due_posts_selects_due_and_skips_rest(social_env):
    now = datetime.now(UTC)
    past = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    future = (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await _seed_task(social_env, "soctest-due", ["publish", "x"], due_datetime=past,
                     description="https://example.com")
    await _seed_task(social_env, "soctest-future", ["publish", "x"], due_datetime=future)
    await _seed_task(social_env, "soctest-noplatform", ["publish"], due_datetime=past)
    await _seed_task(social_env, "soctest-nolabel", ["x"], due_datetime=past)
    # date-only: yesterday at default hour → always past
    await _seed_task(
        social_env, "soctest-dateonly", ["publish", "x"],
        due_date=(now - timedelta(days=1)).date(),
    )
    # already has an outbox row → excluded
    account_id = await _seed_account(social_env)
    await _seed_task(social_env, "soctest-queued", ["publish", "x"], due_datetime=past)
    await social_env.execute(
        "INSERT INTO social_outbox (todoist_task_id, account_id, payload) "
        "VALUES ('soctest-queued', $1, '{}'::jsonb)",
        account_id,
    )

    act = SocialActivities(db_pool=social_env)
    due = await ActivityEnvironment().run(act.find_due_posts, 10, 9)
    by_id = {d["task_id"]: d for d in due}
    assert set(by_id) == {"soctest-due", "soctest-dateonly"}
    assert by_id["soctest-due"]["platforms"] == ["x"]
    assert by_id["soctest-due"]["link"] == "https://example.com"
    # post_at is the resolved due time (ISO, aware) — Postiz scheduling uses it.
    resolved = datetime.fromisoformat(by_id["soctest-due"]["post_at"])
    assert abs((resolved - (now - timedelta(minutes=5))).total_seconds()) < 60


async def test_find_due_posts_rest_api_datetime_fallback(social_env):
    """Rows whose raw due carries the REST-API `datetime` key (instead of the
    Sync API's timed `date`) must still resolve a post_at."""
    past = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await social_env.execute(
        "INSERT INTO todoist_tasks (id, content, labels, raw) VALUES ($1, $2, $3, $4)",
        "soctest-restshape",
        "rest shape",
        ["publish", "x"],
        {"due": {"datetime": past}},
    )
    act = SocialActivities(db_pool=social_env)
    due = await ActivityEnvironment().run(act.find_due_posts, 10, 9)
    assert [d["task_id"] for d in due] == ["soctest-restshape"]


# ------------------------------------------------------- enqueue / drain / complete


async def test_enqueue_outbox_idempotent_and_reports_missing(social_env):
    account_id = await _seed_account(social_env)
    act = SocialActivities(db_pool=social_env)
    env = ActivityEnvironment()
    r1 = await env.run(act.enqueue_outbox, "soctest-t1", ["x", "linkedin"], "hi", "")
    assert r1 == {"queued": 1, "missing_accounts": ["linkedin"]}
    # retry: no duplicate row
    r2 = await env.run(act.enqueue_outbox, "soctest-t1", ["x"], "hi", "")
    assert r2["queued"] == 0
    count = await social_env.fetchval(
        "SELECT count(*) FROM social_outbox WHERE todoist_task_id = 'soctest-t1'"
    )
    assert count == 1
    row = await social_env.fetchrow(
        "SELECT account_id, payload FROM social_outbox WHERE todoist_task_id = 'soctest-t1'"
    )
    assert row["account_id"] == account_id
    assert json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]


async def test_drain_marks_posted_and_counts_failures(social_env):
    account_id = await _seed_account(social_env)
    await social_env.execute(
        "INSERT INTO social_outbox (todoist_task_id, account_id, payload) "
        "VALUES ('soctest-ok', $1, $2)",
        account_id,
        {"text": "hi", "link": ""},
    )
    fake = _FakeConnector(ref="tweet-42")
    act = SocialActivities(db_pool=social_env, connector=fake)
    result = await ActivityEnvironment().run(act.drain_social_outbox)
    assert result == {"posted": 1, "failed": 0}
    row = await social_env.fetchrow(
        "SELECT status, posted_ref FROM social_outbox WHERE todoist_task_id = 'soctest-ok'"
    )
    assert (row["status"], row["posted_ref"]) == ("posted", "tweet-42")

    # failure: bumps attempts, fails permanently at the cap
    await social_env.execute(
        "INSERT INTO social_outbox (todoist_task_id, account_id, payload, attempt_count) "
        "VALUES ('soctest-bad', $1, $2, 3)",
        account_id,
        {"text": "hi", "link": ""},
    )
    act_fail = SocialActivities(db_pool=social_env, connector=_FakeConnector(fail=True))
    assert await ActivityEnvironment().run(act_fail.drain_social_outbox) == {
        "posted": 0,
        "failed": 0,
    }
    assert await ActivityEnvironment().run(act_fail.drain_social_outbox) == {
        "posted": 0,
        "failed": 1,
    }
    status = await social_env.fetchval(
        "SELECT status FROM social_outbox WHERE todoist_task_id = 'soctest-bad'"
    )
    assert status == "failed"


async def test_complete_posted_tasks_enqueues_item_complete_once(social_env):
    account_id = await _seed_account(social_env)
    await _seed_task(social_env, "soctest-done", ["publish", "x"])
    await social_env.execute(
        "INSERT INTO social_outbox (todoist_task_id, account_id, payload, status, posted_ref) "
        "VALUES ('soctest-done', $1, '{}'::jsonb, 'posted', 'ref-9')",
        account_id,
    )
    act = SocialActivities(db_pool=social_env)
    env = ActivityEnvironment()
    assert (await env.run(act.complete_posted_tasks))["completed"] == 1
    assert (await env.run(act.complete_posted_tasks))["completed"] == 0  # idempotent
    row = await social_env.fetchrow(
        "SELECT command FROM todoist_outbox WHERE temp_id = 'social-complete-soctest-done'"
    )
    cmd = row["command"] if isinstance(row["command"], dict) else json.loads(row["command"])
    assert cmd["type"] == "item_complete"
    assert cmd["args"]["id"] == "soctest-done"


async def test_complete_posted_tasks_waits_for_all_platforms(social_env):
    a1 = await _seed_account(social_env, label="one")
    a2 = await _seed_account(social_env, label="two")
    await _seed_task(social_env, "soctest-partial", ["publish", "x"])
    await social_env.execute(
        "INSERT INTO social_outbox (todoist_task_id, account_id, payload, status) "
        "VALUES ('soctest-partial', $1, '{}'::jsonb, 'posted'), "
        "       ('soctest-partial', $2, '{}'::jsonb, 'pending')",
        a1,
        a2,
    )
    act = SocialActivities(db_pool=social_env)
    assert (await ActivityEnvironment().run(act.complete_posted_tasks))["completed"] == 0


# ---------------------------------------------------------------- approval hook


async def test_apply_social_approval_approve_posts_and_completes(social_env):
    await _seed_account(social_env)
    await _seed_task(social_env, "soctest-appr", ["publish", "x"])
    fake = _FakeConnector(ref="tweet-77")
    act = SocialActivities(db_pool=social_env, connector=fake)
    result = await ActivityEnvironment().run(
        act.apply_social_approval,
        "ia-1",
        {"value": "approve"},
        {"task_id": "soctest-appr", "platforms": ["x"], "text": "hi", "link": ""},
    )
    assert result == {"applied": "approved"}
    assert fake.calls and fake.calls[0][1]["text"] == "hi"
    status = await social_env.fetchval(
        "SELECT status FROM social_outbox WHERE todoist_task_id = 'soctest-appr'"
    )
    assert status == "posted"
    assert await social_env.fetchval(
        "SELECT count(*) FROM todoist_outbox WHERE temp_id = 'social-complete-soctest-appr'"
    ) == 1


async def test_apply_social_approval_rereads_current_due_over_stale_snapshot(social_env):
    """The card snapshots post_at at creation; the due can move before approval
    (reschedule, or sync landing after a same-instant social tick). Approval
    must schedule at the CURRENT mirrored due, not the snapshot."""
    await _seed_account(social_env)
    current_due = (datetime.now(UTC) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    await _seed_task(social_env, "soctest-moved", ["publish", "x"], due_datetime=current_due)
    stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    fake = _FakeConnector(ref="tweet-88")
    act = SocialActivities(db_pool=social_env, connector=fake)
    result = await ActivityEnvironment().run(
        act.apply_social_approval,
        "ia-3",
        {"value": "approve"},
        {"task_id": "soctest-moved", "platforms": ["x"], "text": "hi", "link": "",
         "post_at": stale},
    )
    assert result == {"applied": "approved"}
    payload = fake.calls[0][1]
    assert payload["schedule_at"] == f"{current_due}+00:00"  # user_tz=UTC in fixture


async def test_apply_social_approval_skip_strips_publish_label(social_env):
    await _seed_task(social_env, "soctest-skip", ["publish", "x"])
    act = SocialActivities(db_pool=social_env)
    result = await ActivityEnvironment().run(
        act.apply_social_approval, "ia-2", {"value": "skip"}, {"task_id": "soctest-skip"}
    )
    assert result == {"applied": "skipped"}
    labels = await social_env.fetchval(
        "SELECT labels FROM todoist_tasks WHERE id = 'soctest-skip'"
    )
    assert labels == ["x"]  # projection updated optimistically
    row = await social_env.fetchrow(
        "SELECT command FROM todoist_outbox WHERE temp_id = 'social-skip-soctest-skip'"
    )
    cmd = row["command"] if isinstance(row["command"], dict) else json.loads(row["command"])
    assert cmd["type"] == "item_update"
    assert cmd["args"]["labels"] == ["x"]


# ------------------------------------------------------------- connector refresh


@respx.mock
async def test_refresh_rotates_and_persists_before_use(social_env):
    """X invalidates the old refresh token on refresh — the rotated pair must
    be persisted even when the post itself then fails."""
    account_id = await _seed_account(social_env, expires_in_seconds=60)  # inside margin
    respx.post("https://api.x.com/2/oauth2/token").respond(
        200, json={"access_token": "new-acc", "refresh_token": "new-ref", "expires_in": 7200}
    )
    respx.post("https://api.x.com/2/tweets").respond(500, json={"detail": "boom"})

    connector = SocialConnector(db_pool=social_env, settings=_settings())
    try:
        with pytest.raises(RuntimeError):
            await connector.post(account_id, {"text": "hi", "link": ""})
        row = await social_env.fetchrow(
            "SELECT access_token_enc, refresh_token_enc, expires_at "
            "FROM social_accounts WHERE id = $1",
            account_id,
        )
        assert decrypt_secret(row["refresh_token_enc"], "") == "new-ref"
        assert decrypt_secret(row["access_token_enc"], "") == "new-acc"
        assert row["expires_at"] > datetime.now(UTC) + timedelta(minutes=30)
    finally:
        await connector.close()


@respx.mock
async def test_post_x_uses_refreshed_token_and_returns_ref(social_env):
    account_id = await _seed_account(social_env, expires_in_seconds=60)
    respx.post("https://api.x.com/2/oauth2/token").respond(
        200, json={"access_token": "new-acc", "refresh_token": "new-ref", "expires_in": 7200}
    )
    tweets = respx.post("https://api.x.com/2/tweets").respond(
        201, json={"data": {"id": "1234567890"}}
    )
    connector = SocialConnector(db_pool=social_env, settings=_settings())
    try:
        ref = await connector.post(
            account_id, {"text": "hello", "link": "https://example.com"}
        )
        assert ref == "1234567890"
        req = tweets.calls[0].request
        assert req.headers["authorization"] == "Bearer new-acc"
        assert json.loads(req.content) == {"text": "hello\n\nhttps://example.com"}
    finally:
        await connector.close()


@respx.mock
async def test_post_x_skips_refresh_when_token_fresh(social_env):
    account_id = await _seed_account(social_env, expires_in_seconds=3600)
    token_route = respx.post("https://api.x.com/2/oauth2/token").mock(
        return_value=Response(200, json={})
    )
    respx.post("https://api.x.com/2/tweets").respond(201, json={"data": {"id": "9"}})
    connector = SocialConnector(db_pool=social_env, settings=_settings())
    try:
        assert await connector.post(account_id, {"text": "hi", "link": ""}) == "9"
        assert not token_route.called
    finally:
        await connector.close()


# ------------------------------------------------------------------- postiz


@respx.mock
async def test_post_routes_postiz_account_with_raw_auth_header_no_x_calls(social_env):
    """Postiz-mirrored accounts have no OAuth tokens — post() must skip refresh
    entirely and never touch x.com. respx's default assert_all_mocked means an
    unmocked call to any x.com route would fail this test outright."""
    account_id = await _seed_postiz_account(social_env, platform="mastodon", integration_id="int-1")
    posts_route = respx.post("https://postiz.example.com/api/public/v1/posts").respond(
        200, json=[{"postId": "pz-1", "integration": "int-1"}]
    )

    connector = SocialConnector(db_pool=social_env, settings=_settings_with_postiz())
    try:
        ref = await connector.post(
            account_id, {"text": "hello world", "link": "https://example.com"}
        )
        assert ref == "pz-1"

        req = posts_route.calls[0].request
        assert req.headers["authorization"] == "pz-key"  # raw key, no "Bearer " prefix
        body = json.loads(req.content)
        assert body["type"] == "now"
        assert body["posts"][0]["integration"]["id"] == "int-1"
        assert body["posts"][0]["settings"]["__type"] == "mastodon"
        content = body["posts"][0]["value"][0]["content"]
        assert "hello world" in content
        assert "https://example.com" in content
    finally:
        await connector.close()


@respx.mock
async def test_post_postiz_future_schedule_at_schedules_instead_of_now(social_env):
    """A payload with a future schedule_at (the Todoist due time) must become a
    Postiz SCHEDULED post at exactly that moment; a past one posts now."""
    account_id = await _seed_postiz_account(social_env, platform="mastodon", integration_id="int-9")
    posts_route = respx.post("https://postiz.example.com/api/public/v1/posts").respond(
        200, json=[{"postId": "pz-sched", "integration": "int-9"}]
    )
    future = datetime.now(UTC) + timedelta(hours=3)
    connector = SocialConnector(db_pool=social_env, settings=_settings_with_postiz())
    try:
        ref = await connector.post(
            account_id,
            {"text": "later", "link": "", "schedule_at": future.isoformat()},
        )
        assert ref == "pz-sched"
        body = json.loads(posts_route.calls[0].request.content)
        assert body["type"] == "schedule"
        assert body["date"] == future.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        # Past schedule_at (late approval) → immediate post.
        past = datetime.now(UTC) - timedelta(minutes=30)
        await connector.post(
            account_id, {"text": "late", "link": "", "schedule_at": past.isoformat()}
        )
        body2 = json.loads(posts_route.calls[1].request.content)
        assert body2["type"] == "now"
    finally:
        await connector.close()


@respx.mock
async def test_post_postiz_missing_config_raises(social_env):
    account_id = await _seed_postiz_account(social_env)
    connector = SocialConnector(db_pool=social_env, settings=_settings())  # no postiz_url/key
    try:
        with pytest.raises(RuntimeError):
            await connector.post(account_id, {"text": "hi", "link": ""})
    finally:
        await connector.close()


@respx.mock
async def test_drain_social_outbox_posts_via_postiz_end_to_end(social_env):
    account_id = await _seed_postiz_account(social_env, label="drain-postiz")
    await social_env.execute(
        "INSERT INTO social_outbox (todoist_task_id, account_id, payload) "
        "VALUES ('soctest-postiz', $1, $2)",
        account_id,
        {"text": "hi", "link": ""},
    )
    respx.post("https://postiz.example.com/api/public/v1/posts").respond(
        200, json=[{"postId": "pz-2", "integration": "int-1"}]
    )
    connector = SocialConnector(db_pool=social_env, settings=_settings_with_postiz())
    act = SocialActivities(db_pool=social_env, connector=connector)
    try:
        result = await ActivityEnvironment().run(act.drain_social_outbox)
        assert result == {"posted": 1, "failed": 0}
        row = await social_env.fetchrow(
            "SELECT status, posted_ref FROM social_outbox WHERE todoist_task_id = 'soctest-postiz'"
        )
        assert (row["status"], row["posted_ref"]) == ("posted", "pz-2")
    finally:
        await connector.close()
