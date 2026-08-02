"""Issue #221 — approving a persona draft whose base document moved is refused.

`ProfileReflectionFlow` proposes a WHOLE-DOCUMENT rewrite of an agent's `user`
persona doc, computed from that doc as it read at proposal time, and parks it as
a `draft_review` card for up to seven days. If the document changes in between —
a hand edit through the admin UI, or another approved card — approving the old
draft discards that change in full. It used to be applied anyway, with a warning
line nobody reads.

The resolve endpoint is the single choke-point for every human response, so the
check lives there: an approve against a stale base is refused with a 409 whose
body carries the CURRENT document, and the card stays pending. Approving again
with `base_ack` set to the fingerprint the 409 handed back is the explicit "I
read that and still mean it".

Everything below drives the real route against a real Postgres and asserts the
409 BODY and the row state — a status-code-only assertion would pass with the
handler gutted. Rows are prefixed `zz221-`.
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.api.routes.interactions import get_workflow_client
from aegis.config import Settings
from aegis.services.personalities import doc_fingerprint, invalidate
from httpx import ASGITransport, AsyncClient

AGENT = "zz221-drift"

# Long enough that a "current document" is unmistakably a document.
BASE_DOC = "Owner is based in Pune.\nPrefers concise answers.\n" + ("x" * 400)
HAND_EDIT = BASE_DOC + "\nOwner moved to Bengaluru in July."
PROPOSED_DOC = BASE_DOC + "\nRuns a homelab swarm called meem."

_TEST_REQUIRED_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "n8n_ui_url": "https://n8n.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
    "n8n_webhook_secret": "test-secret",
}


@pytest.fixture
def auth_headers():
    return {"Authorization": "Basic " + base64.b64encode(b"admin:admin").decode()}


async def _wipe(conn):
    await conn.execute("DELETE FROM interactions WHERE agent_id = $1", AGENT)
    await conn.execute("DELETE FROM agent_memory WHERE agent_id = $1", AGENT)
    await conn.execute("DELETE FROM agent_profile_revisions WHERE agent_id = $1", AGENT)
    await conn.execute("DELETE FROM agent_personalities WHERE agent_id = $1", AGENT)


@pytest_asyncio.fixture(loop_scope="function")
async def app_and_pool(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, role, system_prompt_path, active) "
            "VALUES ($1, $1, 'assistant', 'personalities/x', TRUE) "
            "ON CONFLICT (id) DO NOTHING",
            AGENT,
        )
        await _wipe(conn)
    await _set_doc(db_pool, BASE_DOC)

    app = create_app(run_lifespan=False)
    app.state.db_pool = db_pool
    fake_handle = AsyncMock()
    fake_client = AsyncMock()
    fake_client.get_workflow_handle = lambda wid: fake_handle
    app.dependency_overrides[get_settings] = lambda: Settings(**_TEST_REQUIRED_SETTINGS)
    app.dependency_overrides[get_workflow_client] = lambda: fake_client

    yield app, db_pool, fake_handle

    async with db_pool.acquire() as conn:
        await _wipe(conn)
        await conn.execute("DELETE FROM agents WHERE id = $1", AGENT)
    invalidate(AGENT)


async def _set_doc(pool, content: str):
    """The hand-edit path — exactly what an operator saving the Agents page does."""
    await pool.execute(
        "INSERT INTO agent_personalities (agent_id, kind, content) VALUES ($1,'user',$2) "
        "ON CONFLICT (agent_id, kind) DO UPDATE SET content = EXCLUDED.content",
        AGENT,
        content,
    )
    invalidate(AGENT)


async def _card(pool) -> str:
    """A pending draft_review carrying the metadata ProfileReflectionFlow writes."""
    return await pool.fetchval(
        "INSERT INTO interactions "
        "(flow_run_id, agent_id, kind, origin, prompt, status, timeout_policy, metadata) "
        "VALUES ($1, $2, 'draft_review', 'profile_reflection', 'Weekly profile draft', "
        " 'pending', 'archive', $3) RETURNING id",
        f"zz221-{uuid4()}",
        AGENT,
        {
            "agent_id": AGENT,
            "kind": "user",
            "proposed_doc": PROPOSED_DOC,
            "revision_of": doc_fingerprint(BASE_DOC),
        },
    )


async def _resolve(app, headers, interaction_id, payload):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            f"/api/interactions/{interaction_id}/resolve",
            json={"response": payload},
            headers=headers,
        )


async def _row(pool, interaction_id):
    return await pool.fetchrow(
        "SELECT status, response FROM interactions WHERE id = $1", interaction_id
    )


async def test_approving_a_drifted_draft_is_refused_and_shows_the_current_document(
    app_and_pool, auth_headers
):
    """The defect: the card was proposed against BASE_DOC, the owner hand-edited
    the doc, and approving used to silently replace the edit."""
    app, pool, fake_handle = app_and_pool
    card = await _card(pool)
    await _set_doc(pool, HAND_EDIT)

    resp = await _resolve(
        app, auth_headers, card, {"action": "approve", "edited_doc": PROPOSED_DOC}
    )

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "profile_base_drift"
    assert detail["agent_id"] == AGENT
    assert detail["kind"] == "user"
    # The whole point: the approver is shown the text their approval would have
    # discarded, not just told "conflict".
    assert detail["current_doc"] == HAND_EDIT
    assert detail["proposed_from"] == doc_fingerprint(BASE_DOC)

    # Nothing was resolved, nothing was signalled — the card is still answerable.
    row = await _row(pool, card)
    assert row["status"] == "pending"
    assert row["response"] is None
    fake_handle.signal.assert_not_called()


async def test_approving_an_undrifted_draft_still_resolves(app_and_pool, auth_headers):
    """The guard is conditional on the document having MOVED. Without this, a
    guard that refused every approve would look identical to a working one."""
    app, pool, fake_handle = app_and_pool
    card = await _card(pool)

    resp = await _resolve(
        app, auth_headers, card, {"action": "approve", "edited_doc": PROPOSED_DOC}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["already_resolved"] is False
    assert (await _row(pool, card))["status"] == "resolved"
    fake_handle.signal.assert_awaited_once()


async def test_acknowledging_the_current_document_unlocks_the_approval(
    app_and_pool, auth_headers
):
    """The escape hatch. Without it a drifted card could never be approved at
    all, so the weekly flow would stay blocked on it until it archived."""
    app, pool, fake_handle = app_and_pool
    card = await _card(pool)
    await _set_doc(pool, HAND_EDIT)

    refused = await _resolve(
        app, auth_headers, card, {"action": "approve", "edited_doc": PROPOSED_DOC}
    )
    assert refused.status_code == 409

    # Exactly what the panel resubmits: the fingerprint the 409 handed back.
    resp = await _resolve(
        app,
        auth_headers,
        card,
        {
            "action": "approve",
            "edited_doc": PROPOSED_DOC,
            "base_ack": refused.json()["detail"]["current"],
        },
    )

    assert resp.status_code == 200, resp.text
    assert (await _row(pool, card))["status"] == "resolved"
    fake_handle.signal.assert_awaited_once()


async def test_an_ack_that_does_not_match_the_live_document_is_still_refused(
    app_and_pool, auth_headers
):
    """`base_ack` must name the document the human was actually shown. A client
    that just sets the key — or one acking a fingerprint that has since moved
    again — gets the 409 back."""
    app, pool, _ = app_and_pool
    card = await _card(pool)
    await _set_doc(pool, HAND_EDIT)

    resp = await _resolve(
        app,
        auth_headers,
        card,
        {"action": "approve", "edited_doc": PROPOSED_DOC, "base_ack": "0000deadbeef0000"},
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "profile_base_drift"
    assert (await _row(pool, card))["status"] == "pending"


async def test_rejecting_a_drifted_draft_is_not_blocked(app_and_pool, auth_headers):
    """A reject writes nothing to the profile, so a stale base is irrelevant —
    and blocking it would trap the card. The reason still has to reach the
    learning loop."""
    app, pool, fake_handle = app_and_pool
    card = await _card(pool)
    await _set_doc(pool, HAND_EDIT)

    resp = await _resolve(
        app, auth_headers, card, {"action": "reject", "reason": "already fixed by hand"}
    )

    assert resp.status_code == 200, resp.text
    assert (await _row(pool, card))["status"] == "resolved"
    fake_handle.signal.assert_awaited_once()


async def test_a_non_profile_card_is_never_gated_by_the_drift_check(
    app_and_pool, auth_headers
):
    """Every other interaction kind resolves exactly as before — the guard keys
    on `metadata.revision_of`, which only a persona draft carries. An `approval`
    card for this same agent, whose doc HAS drifted, still goes through."""
    app, pool, fake_handle = app_and_pool
    card = await pool.fetchval(
        "INSERT INTO interactions "
        "(flow_run_id, agent_id, kind, origin, prompt, status, timeout_policy, metadata) "
        "VALUES ($1, $2, 'approval', 'zz221', 'Ship it?', 'pending', 'archive', $3) "
        "RETURNING id",
        f"zz221-{uuid4()}",
        AGENT,
        {"agent_id": AGENT, "kind": "user"},
    )
    await _set_doc(pool, HAND_EDIT)

    resp = await _resolve(app, auth_headers, card, {"action": "approve"})

    assert resp.status_code == 200, resp.text
    assert (await _row(pool, card))["status"] == "resolved"
    fake_handle.signal.assert_awaited_once()
