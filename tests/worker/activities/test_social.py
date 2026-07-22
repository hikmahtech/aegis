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
from aegis_worker.activities.social import SocialActivities, _normalize_link
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
    # The connector now reads Postiz creds FRESH from these keys — clear any
    # ambient dev-DB config during tests so the fresh-read falls back to the
    # env/settings snapshot (restored on teardown).
    "integration:postiz_url",
    "integration:postiz_api_key",
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
        await conn.execute(
            "DELETE FROM settings WHERE key = ANY($1::text[])",
            ["integration:postiz_url", "integration:postiz_api_key"],
        )
    yield db_pool
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM social_outbox")
        await conn.execute("DELETE FROM social_accounts")
        await conn.execute("DELETE FROM todoist_outbox WHERE temp_id LIKE 'social-%'")
        await conn.execute("DELETE FROM todoist_tasks WHERE id LIKE 'soctest-%'")
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


async def test_find_due_posts_postiz_platforms_card_immediately(social_env):
    """A task whose labeled platforms are ALL Postiz-mirrored ignores the
    lookahead — Postiz holds the schedule, so it cards on creation (#60)."""
    future = (datetime.now(UTC) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await _seed_postiz_account(social_env, platform="x")
    await _seed_task(social_env, "soctest-postiz-far", ["publish", "x"], due_datetime=future)

    act = SocialActivities(db_pool=social_env)
    due = await ActivityEnvironment().run(act.find_due_posts, 10, 9)
    assert [d["task_id"] for d in due] == ["soctest-postiz-far"]
    # post_at survives untouched — Postiz schedules for exactly this moment
    resolved = datetime.fromisoformat(due[0]["post_at"])
    assert abs((resolved - (datetime.now(UTC) + timedelta(days=3))).total_seconds()) < 60


async def test_find_due_posts_mixed_platforms_stay_just_in_time(social_env):
    """One native platform in the label set keeps the whole task on the
    just-in-time window — native transports post immediately on approval."""
    now = datetime.now(UTC)
    future = (now + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    near = (now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await _seed_postiz_account(social_env, platform="x")
    await _seed_account(social_env, platform="linkedin")  # native tokens
    await _seed_task(
        social_env, "soctest-mixed-far", ["publish", "x", "linkedin"], due_datetime=future
    )
    await _seed_task(
        social_env, "soctest-mixed-near", ["publish", "x", "linkedin"], due_datetime=near
    )

    act = SocialActivities(db_pool=social_env)
    due = await ActivityEnvironment().run(act.find_due_posts, 10, 9)
    assert [d["task_id"] for d in due] == ["soctest-mixed-near"]


# ---------------------------------------------------- link normalization (#114)


def test_normalize_link_reduces_markdown_link_field_to_bare_url():
    """Prod shape (a): 15/24 posts have `link` populated as full markdown."""
    text, link = _normalize_link(
        "Big launch today", "[Read the announcement](https://example.com/launch)"
    )
    assert text == "Big launch today"
    assert link == "https://example.com/launch"


def test_normalize_link_extracts_markdown_link_from_body_when_link_empty():
    """Prod shape (b): 9/24 posts have `link=""` and the markdown link
    embedded in the body text instead."""
    text, link = _normalize_link(
        "Big news! [Read the announcement](https://example.com/launch) — check it out.",
        "",
    )
    assert link == "https://example.com/launch"
    assert text == "Big news! Read the announcement — check it out."


def test_normalize_link_leaves_bare_url_link_field_untouched():
    assert _normalize_link("hello", "https://example.com") == ("hello", "https://example.com")


def test_normalize_link_leaves_bare_url_in_body_untouched_when_link_empty():
    assert _normalize_link("Check https://example.com/foo out", "") == (
        "Check https://example.com/foo out",
        "",
    )


def test_normalize_link_no_link_anywhere_is_noop():
    assert _normalize_link("just text", "") == ("just text", "")


async def test_find_due_posts_normalizes_markdown_link_field(social_env):
    """Ingest-side fix for prod shape (a) via find_due_posts (#114)."""
    past = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await _seed_task(
        social_env,
        "soctest-md-link",
        ["publish", "x"],
        due_datetime=past,
        content="Big launch today",
        description="[Read the announcement](https://example.com/launch)",
    )
    act = SocialActivities(db_pool=social_env)
    due = await ActivityEnvironment().run(act.find_due_posts, 10, 9)
    by_id = {d["task_id"]: d for d in due}
    assert by_id["soctest-md-link"]["link"] == "https://example.com/launch"
    assert by_id["soctest-md-link"]["text"] == "Big launch today"


async def test_find_due_posts_extracts_markdown_link_from_body(social_env):
    """Ingest-side fix for prod shape (b) via find_due_posts (#114)."""
    past = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await _seed_task(
        social_env,
        "soctest-md-body",
        ["publish", "x"],
        due_datetime=past,
        content="Big news! [Read the announcement](https://example.com/launch) — check it out.",
        description="",
    )
    act = SocialActivities(db_pool=social_env)
    due = await ActivityEnvironment().run(act.find_due_posts, 10, 9)
    by_id = {d["task_id"]: d for d in due}
    assert by_id["soctest-md-body"]["link"] == "https://example.com/launch"
    assert (
        by_id["soctest-md-body"]["text"]
        == "Big news! Read the announcement — check it out."
    )


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


async def test_apply_social_approval_missing_account_does_not_complete(social_env):
    """Fix #3: a labeled platform with no connected account must NOT complete
    the task — it stays open (visible) and the gap is surfaced as a comment."""
    await _seed_account(social_env, platform="x")  # only x connected; linkedin absent
    await _seed_task(social_env, "soctest-miss", ["publish", "x", "linkedin"])
    fake = _FakeConnector(ref="tweet-x")
    act = SocialActivities(db_pool=social_env, connector=fake)
    result = await ActivityEnvironment().run(
        act.apply_social_approval,
        "ia-miss",
        {"value": "approve"},
        {"task_id": "soctest-miss", "platforms": ["x", "linkedin"], "text": "hi", "link": ""},
    )
    assert result == {"applied": "approved"}
    # x still posted…
    assert await social_env.fetchval(
        "SELECT status FROM social_outbox WHERE todoist_task_id = 'soctest-miss'"
    ) == "posted"
    # …but the task is NOT completed (linkedin never went out).
    assert await social_env.fetchval(
        "SELECT count(*) FROM todoist_outbox WHERE temp_id = 'social-complete-soctest-miss'"
    ) == 0
    # gap surfaced as a Todoist comment
    note = await social_env.fetchrow(
        "SELECT command FROM todoist_outbox WHERE temp_id = 'social-missing-soctest-miss-linkedin'"
    )
    assert note is not None
    cmd = note["command"] if isinstance(note["command"], dict) else json.loads(note["command"])
    assert cmd["type"] == "note_add"
    assert "linkedin" in cmd["args"]["content"]


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


# --------------------------------------- postiz linkedin first-comment (#83)


@respx.mock
async def test_postiz_linkedin_link_becomes_first_comment(social_env):
    """LinkedIn penalizes in-body links — text and link ship as two `value`
    items so Postiz's LinkedIn provider posts the link as a first comment."""
    account_id = await _seed_postiz_account(social_env, platform="linkedin", integration_id="int-li")
    route = respx.post("https://postiz.example.com/api/public/v1/posts").respond(
        200, json=[{"postId": "pz-1", "integration": "int-li"}]
    )
    connector = SocialConnector(db_pool=social_env, settings=_settings_with_postiz())
    try:
        ref = await connector.post(
            account_id, {"text": "Big launch today", "link": "https://example.com/post"}
        )
        assert ref == "pz-1"
        body = json.loads(route.calls[0].request.content)
        assert body["posts"][0]["value"] == [
            {"content": "Big launch today", "image": []},
            {"content": "https://example.com/post", "image": []},
        ]
    finally:
        await connector.close()


@respx.mock
async def test_postiz_linkedin_page_also_splits(social_env):
    """The `linkedin-page` identifier (company pages) gets the same treatment
    as personal `linkedin` accounts."""
    account_id = await _seed_postiz_account(
        social_env, platform="linkedin-page", integration_id="int-lip"
    )
    route = respx.post("https://postiz.example.com/api/public/v1/posts").respond(
        200, json=[{"postId": "pz-2", "integration": "int-lip"}]
    )
    connector = SocialConnector(db_pool=social_env, settings=_settings_with_postiz())
    try:
        await connector.post(account_id, {"text": "hi", "link": "https://l.example"})
        body = json.loads(route.calls[0].request.content)
        assert body["posts"][0]["value"] == [
            {"content": "hi", "image": []},
            {"content": "https://l.example", "image": []},
        ]
    finally:
        await connector.close()


@respx.mock
async def test_postiz_linkedin_no_link_single_item(social_env):
    """No link at all → unchanged single-item body, same as every other platform."""
    account_id = await _seed_postiz_account(social_env, platform="linkedin", integration_id="int-li2")
    route = respx.post("https://postiz.example.com/api/public/v1/posts").respond(
        200, json=[{"postId": "pz-3", "integration": "int-li2"}]
    )
    connector = SocialConnector(db_pool=social_env, settings=_settings_with_postiz())
    try:
        await connector.post(account_id, {"text": "no link here", "link": ""})
        body = json.loads(route.calls[0].request.content)
        assert body["posts"][0]["value"] == [{"content": "no link here", "image": []}]
    finally:
        await connector.close()


@respx.mock
async def test_postiz_linkedin_link_only_stays_in_body(social_env):
    """A link with no body text can't become a first comment on nothing — it
    stays as the sole (in-body) value item, matching _render_text."""
    account_id = await _seed_postiz_account(social_env, platform="linkedin", integration_id="int-li3")
    route = respx.post("https://postiz.example.com/api/public/v1/posts").respond(
        200, json=[{"postId": "pz-4", "integration": "int-li3"}]
    )
    connector = SocialConnector(db_pool=social_env, settings=_settings_with_postiz())
    try:
        await connector.post(account_id, {"text": "", "link": "https://only.example"})
        body = json.loads(route.calls[0].request.content)
        assert body["posts"][0]["value"] == [{"content": "https://only.example", "image": []}]
    finally:
        await connector.close()


@respx.mock
async def test_postiz_non_linkedin_keeps_link_in_body(social_env):
    """Non-LinkedIn platforms are unaffected — link stays appended in-body."""
    account_id = await _seed_postiz_account(social_env, platform="x", integration_id="int-x")
    route = respx.post("https://postiz.example.com/api/public/v1/posts").respond(
        200, json=[{"postId": "pz-5", "integration": "int-x"}]
    )
    connector = SocialConnector(db_pool=social_env, settings=_settings_with_postiz())
    try:
        await connector.post(account_id, {"text": "tweet", "link": "https://t.example"})
        body = json.loads(route.calls[0].request.content)
        assert body["posts"][0]["value"] == [
            {"content": "tweet\n\nhttps://t.example", "image": []}
        ]
    finally:
        await connector.close()


# ------------------------------------------------- postiz per-platform settings


async def _post_via_postiz(social_env, platform: str, meta: dict, text: str = "hi") -> dict:
    """Route one post through the real connector, respx-mocking Postiz, and
    return the `settings` object it sent."""
    account_id = await social_env.fetchval(
        "INSERT INTO social_accounts (platform, label, meta) VALUES ($1, $2, $3) RETURNING id",
        platform,
        f"{platform}-acct",
        {"postiz_integration_id": f"int-{platform}", **meta},
    )
    route = respx.post("https://postiz.example.com/api/public/v1/posts").respond(
        200, json=[{"postId": "pz-1", "integration": f"int-{platform}"}]
    )
    connector = SocialConnector(db_pool=social_env, settings=_settings_with_postiz())
    try:
        await connector.post(account_id, {"text": text, "link": ""})
        return json.loads(route.calls[0].request.content)["posts"][0]["settings"]
    finally:
        await connector.close()


@respx.mock
async def test_post_postiz_x_includes_required_who_can_reply_post(social_env):
    """XDto.who_can_reply_post is required (non-@IsOptional) — omitting it 400s."""
    settings = await _post_via_postiz(social_env, "x", {})
    assert settings["__type"] == "x"
    assert settings["who_can_reply_post"] == "everyone"


@respx.mock
@pytest.mark.parametrize("platform", ["facebook", "linkedin", "mastodon"])
async def test_post_postiz_all_optional_platforms_send_only_type(social_env, platform):
    """DTOs with only @IsOptional fields must keep validating on `__type` alone
    — this is the already-connected LinkedIn/Facebook contract; don't break it."""
    settings = await _post_via_postiz(social_env, platform, {})
    assert settings == {"__type": platform}


@respx.mock
async def test_post_postiz_youtube_sets_required_title_and_type(social_env):
    """YoutubeSettingsDto requires title (>=2 chars) and type (@IsIn)."""
    text = "A reasonably long YouTube description that exceeds the ninety char title cap " * 2
    settings = await _post_via_postiz(social_env, "youtube", {}, text=text)
    assert settings["__type"] == "youtube"
    assert settings["type"] == "public"
    assert settings["title"] == text.strip()[:90]
    assert 2 <= len(settings["title"]) <= 100


@respx.mock
async def test_post_postiz_meta_override_wins_over_defaults(social_env):
    """meta.postiz_settings pins/overrides per-account settings."""
    settings = await _post_via_postiz(
        social_env, "x", {"postiz_settings": {"who_can_reply_post": "verified"}}
    )
    assert settings["who_can_reply_post"] == "verified"


async def test_post_postiz_reddit_without_subreddit_raises(social_env):
    """Reddit needs an operator-supplied subreddit array we can't synthesize —
    raise a clear error (caught per-row by drain) rather than send a 400 body."""
    account_id = await _seed_postiz_account(social_env, platform="reddit", integration_id="int-r")
    connector = SocialConnector(db_pool=social_env, settings=_settings_with_postiz())
    try:
        with pytest.raises(RuntimeError, match="subreddit"):
            await connector.post(account_id, {"text": "hi", "link": ""})
    finally:
        await connector.close()


@respx.mock
async def test_post_postiz_reddit_with_meta_subreddit_posts(social_env):
    subreddit = [
        {"value": {"subreddit": "/r/test", "title": "hi", "type": "self",
                   "is_flair_required": False}}
    ]
    settings = await _post_via_postiz(
        social_env, "reddit", {"postiz_settings": {"subreddit": subreddit}}
    )
    assert settings["__type"] == "reddit"
    assert settings["subreddit"] == subreddit


@respx.mock
async def test_postiz_creds_read_fresh_from_db_over_settings_snapshot(social_env):
    """Fix #2: admin-UI Postiz credential edits land in the settings table; the
    connector must read them FRESH per call, not the boot-time settings snapshot
    (which pins whatever was configured at worker startup)."""
    account_id = await _seed_postiz_account(social_env, platform="mastodon", integration_id="int-1")
    # DB config points at a DIFFERENT host + key than the settings snapshot.
    await social_env.execute(
        "INSERT INTO settings (key, value) VALUES ('integration:postiz_url', $1), "
        "('integration:postiz_api_key', $2) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        {"val": "https://db-postiz.example.com"},
        {"enc": encrypt_secret("db-key", "")},  # settings.secret_key="" → plaintext mode
    )
    route = respx.post("https://db-postiz.example.com/api/public/v1/posts").respond(
        200, json=[{"postId": "pz-db", "integration": "int-1"}]
    )
    # The snapshot still has the OLD postiz.example.com host — a stale read would
    # POST there and this test's db-postiz route would never be called.
    connector = SocialConnector(db_pool=social_env, settings=_settings_with_postiz())
    try:
        assert await connector.post(account_id, {"text": "hi", "link": ""}) == "pz-db"
        assert route.calls[0].request.headers["authorization"] == "db-key"
    finally:
        await connector.close()


# ------------------------------------------------------------ metrics: connector


@respx.mock
async def test_get_post_metrics_normalizes_series_and_handles_empty(social_env):
    respx.get("https://postiz.example.com/api/public/v1/analytics/post/pz-1").respond(
        200,
        json=[
            {"label": "Likes", "data": [{"total": "150", "date": "2025-01-01"}], "percentageChange": 16.7}
        ],
    )
    respx.get("https://postiz.example.com/api/public/v1/analytics/post/pz-2").respond(200, json=[])
    connector = SocialConnector(db_pool=social_env, settings=_settings_with_postiz())
    try:
        result = await connector.get_post_metrics("pz-1")
        assert result["series"] == {"likes": 150}
        assert result["raw_labels"] == ["Likes"]
        assert await connector.get_post_metrics("pz-2") == {"series": {}}
    finally:
        await connector.close()


async def test_get_post_metrics_missing_config_raises(social_env):
    connector = SocialConnector(db_pool=social_env, settings=_settings())
    try:
        with pytest.raises(RuntimeError):
            await connector.get_post_metrics("pz-1")
    finally:
        await connector.close()


@respx.mock
async def test_list_posts_window_tolerates_dict_shape(social_env):
    respx.get("https://postiz.example.com/api/public/v1/posts").respond(
        200, json={"posts": [{"id": "1"}]}
    )
    connector = SocialConnector(db_pool=social_env, settings=_settings_with_postiz())
    try:
        assert await connector.list_posts_window("2026-01-01", "2026-01-02") == [{"id": "1"}]
    finally:
        await connector.close()


@respx.mock
async def test_list_posts_window_tolerates_bare_array_shape(social_env):
    respx.get("https://postiz.example.com/api/public/v1/posts").respond(
        200, json=[{"id": "2"}]
    )
    connector = SocialConnector(db_pool=social_env, settings=_settings_with_postiz())
    try:
        assert await connector.list_posts_window("2026-01-01", "2026-01-02") == [{"id": "2"}]
    finally:
        await connector.close()


async def test_list_posts_window_missing_config_raises(social_env):
    connector = SocialConnector(db_pool=social_env, settings=_settings())
    try:
        with pytest.raises(RuntimeError):
            await connector.list_posts_window("2026-01-01", "2026-01-02")
    finally:
        await connector.close()


# ------------------------------------------------------ metrics: refresh_post_metrics


async def test_refresh_post_metrics_no_deps_returns_zero():
    act = SocialActivities(db_pool=None)
    assert await ActivityEnvironment().run(act.refresh_post_metrics, 14) == {
        "refreshed": 0,
        "failed": 0,
    }


@respx.mock
async def test_refresh_post_metrics_updates_state_release_url_series(social_env):
    account_id = await _seed_postiz_account(social_env, platform="mastodon", integration_id="int-1")
    await social_env.execute(
        "INSERT INTO social_outbox (todoist_task_id, account_id, payload, status, posted_ref) "
        "VALUES ('soctest-metrics-ok', $1, $2, 'posted', 'pz-100')",
        account_id,
        {"text": "hi", "link": ""},
    )
    respx.get("https://postiz.example.com/api/public/v1/posts").respond(
        200,
        json={
            "posts": [
                {
                    "id": "pz-100",
                    "state": "PUBLISHED",
                    "releaseURL": "https://mastodon.social/@me/100",
                    "publishDate": "2026-07-01T10:00:00.000Z",
                }
            ]
        },
    )
    respx.get("https://postiz.example.com/api/public/v1/analytics/post/pz-100").respond(
        200,
        json=[
            {"label": "Likes", "data": [{"total": "150", "date": "2026-07-01"}], "percentageChange": 16.7},
            {"label": "Comments", "data": [{"total": "3", "date": "2026-07-01"}], "percentageChange": 0},
        ],
    )

    connector = SocialConnector(db_pool=social_env, settings=_settings_with_postiz())
    act = SocialActivities(db_pool=social_env, connector=connector)
    try:
        result = await ActivityEnvironment().run(act.refresh_post_metrics, 14)
        assert result == {"refreshed": 1, "failed": 0}
        row = await social_env.fetchrow(
            "SELECT metrics, metrics_at FROM social_outbox WHERE todoist_task_id = 'soctest-metrics-ok'"
        )
        assert row["metrics_at"] is not None
        metrics = row["metrics"]
        assert metrics["state"] == "PUBLISHED"
        assert metrics["release_url"] == "https://mastodon.social/@me/100"
        assert metrics["series"] == {"likes": 150, "comments": 3}
    finally:
        await connector.close()


@respx.mock
async def test_refresh_post_metrics_one_failure_does_not_block_others(social_env):
    account_id = await _seed_postiz_account(social_env, platform="mastodon", integration_id="int-1")
    await social_env.execute(
        "INSERT INTO social_outbox (todoist_task_id, account_id, payload, status, posted_ref) "
        "VALUES ('soctest-metrics-bad', $1, $2, 'posted', 'pz-bad'), "
        "       ('soctest-metrics-good', $1, $2, 'posted', 'pz-good')",
        account_id,
        {"text": "hi", "link": ""},
    )
    respx.get("https://postiz.example.com/api/public/v1/posts").respond(200, json={"posts": []})
    respx.get("https://postiz.example.com/api/public/v1/analytics/post/pz-bad").respond(
        500, json={"error": "boom"}
    )
    respx.get("https://postiz.example.com/api/public/v1/analytics/post/pz-good").respond(
        200, json=[{"label": "Likes", "data": [{"total": "5", "date": "2026-07-01"}]}]
    )

    connector = SocialConnector(db_pool=social_env, settings=_settings_with_postiz())
    act = SocialActivities(db_pool=social_env, connector=connector)
    try:
        result = await ActivityEnvironment().run(act.refresh_post_metrics, 14)
        assert result == {"refreshed": 1, "failed": 1}
        good_metrics = await social_env.fetchval(
            "SELECT metrics FROM social_outbox WHERE todoist_task_id = 'soctest-metrics-good'"
        )
        assert good_metrics["series"] == {"likes": 5}
        assert good_metrics["state"] == "unknown"  # not present in list_posts_window's empty map
        bad_metrics = await social_env.fetchval(
            "SELECT metrics FROM social_outbox WHERE todoist_task_id = 'soctest-metrics-bad'"
        )
        assert bad_metrics == {}  # never updated — the row's failure didn't block the good one
    finally:
        await connector.close()


@respx.mock
async def test_refresh_post_metrics_list_posts_failure_continues_with_empty_map(social_env):
    """A failed list_posts_window call must not abort the pass — per-post
    analytics is the core value; state/release_url just stay unknown."""
    account_id = await _seed_postiz_account(social_env)
    await social_env.execute(
        "INSERT INTO social_outbox (todoist_task_id, account_id, payload, status, posted_ref) "
        "VALUES ('soctest-metrics-nolist', $1, $2, 'posted', 'pz-77')",
        account_id,
        {"text": "hi", "link": ""},
    )
    respx.get("https://postiz.example.com/api/public/v1/posts").respond(500, json={"error": "boom"})
    respx.get("https://postiz.example.com/api/public/v1/analytics/post/pz-77").respond(
        200, json=[{"label": "Likes", "data": [{"total": "9", "date": "2026-07-01"}]}]
    )
    connector = SocialConnector(db_pool=social_env, settings=_settings_with_postiz())
    act = SocialActivities(db_pool=social_env, connector=connector)
    try:
        result = await ActivityEnvironment().run(act.refresh_post_metrics, 14)
        assert result == {"refreshed": 1, "failed": 0}
        metrics = await social_env.fetchval(
            "SELECT metrics FROM social_outbox WHERE todoist_task_id = 'soctest-metrics-nolist'"
        )
        assert metrics["state"] == "unknown"
        assert metrics["series"] == {"likes": 9}
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
