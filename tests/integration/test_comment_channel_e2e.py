"""End-to-end: Todoist webhook -> ClarifyFlow -> AgentChatReplyFlow ->
chat dispatch + Todoist comment mirror.

Real Postgres (asyncpg via db_pool fixture). HTTP stubbed via respx
(core /api/chat/agent-reply, comms delivery, Todoist Sync API).

This e2e variant pins the webhook -> DB-bump boundary. The Temporal-driven
ClarifyFlow + AgentChatReplyFlow lifecycle (spawn, child-workflow chain)
is covered by per-component workflow tests in:
  - tests/worker/test_clarify_flow_agent_spawn.py
  - tests/worker/test_agent_chat_reply_flow.py
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json as _json

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from httpx import ASGITransport, AsyncClient

_TEST_REQUIRED_SETTINGS: dict = {
    "database_url": "postgresql://aegis:aegis_dev@localhost:25432/aegis",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "n8n_ui_url": "https://n8n.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
    "todoist_webhook_secret": "test-secret",
    "temporal_host": "",  # bypass Temporal kick — tests don't run a real worker
}


@pytest.fixture
def test_settings() -> Settings:
    return Settings(**_TEST_REQUIRED_SETTINGS)


@pytest_asyncio.fixture(loop_scope="function")
async def _seeded_task_with_raphael(db_pool):
    async with db_pool.acquire() as conn:
        # Ensure inbox project exists (FK from todoist_tasks.project_id)
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('PROJ-INBOX-E2E', 'Inbox', true, '{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING"
        )
        # Clean any prior state for this test's task id
        await conn.execute("DELETE FROM todoist_tasks WHERE id = $1", "task-e2e")
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, content, project_id, source_tag, labels, last_clarified_at) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            "task-e2e",
            "Spike: Tigris vs S3 for cold archives",
            "PROJ-INBOX-E2E",
            "#manual",
            ["@raphael", "#manual"],
            dt.datetime(2025, 1, 1, 0, 0, tzinfo=dt.UTC),
        )
    yield
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM todoist_tasks WHERE id = $1", "task-e2e")


@pytest.mark.asyncio
async def test_user_comment_to_raphael_full_flow(db_pool, test_settings, _seeded_task_with_raphael):
    """Webhook -> SQL filter -> last_note_at bump.

    NOTE: this test pins the webhook -> DB-bump boundary. The remainder of
    the lifecycle (ClarifyFlow tick -> spawn -> AgentChatReplyFlow -> core
    /api/chat/agent-reply -> chat dispatch + post_agent_reply_comment)
    requires a running Temporal worker, which is exercised by the
    per-component workflow tests, not this e2e.
    """
    app = create_app(run_lifespan=False)
    app.state.db_pool = db_pool
    app.dependency_overrides[get_settings] = lambda: test_settings

    payload = {
        "event_name": "note:added",
        "event_data": {
            "item_id": "task-e2e",
            "content": "Tell me what we know about Tigris.",
        },
    }
    body = _json.dumps(payload).encode()
    # Todoist base64-encodes the digest (not hex, unlike GitHub/Sentry).
    signed = base64.b64encode(hmac.new(b"test-secret", body, hashlib.sha256).digest()).decode()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/webhooks/todoist",
            content=body,
            headers={
                "X-Todoist-Hmac-Sha256": signed,
                "Content-Type": "application/json",
            },
        )

    assert resp.status_code == 200

    # last_note_at should be bumped (NOT an agent prefix)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_note_at, last_clarified_at FROM todoist_tasks WHERE id = $1",
            "task-e2e",
        )
    assert row["last_note_at"] is not None
    # last_note_at > last_clarified_at means find_unclassified_items would
    # surface this task on the next ClarifyFlow tick -> classify_one's
    # @raphael short-circuit would fire -> apply_outcome's raphael_followup
    # branch would return spawn payload -> ClarifyFlow would start
    # AgentChatReplyFlow as a child workflow.
    assert row["last_note_at"] > row["last_clarified_at"]
