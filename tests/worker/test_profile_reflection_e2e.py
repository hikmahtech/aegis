"""A2 falsifiability guard — the admin panel's payload IS the applier's contract.

`apply_profile_reflection` reads `response["action"]` and `response["edited_doc"]`.
Nothing in Python produces that dict: it is built in TypeScript, in the
`draft_review` branch of `admin-panel/frontend/src/pages/InteractionDetail.tsx`,
and posted to `POST /api/interactions/{id}/resolve`. Before A2 that branch
posted `{value: draft}` — no `action`, no `edited_doc` — so the applier's
contract could not have fired from the only screen that can answer the card.

A test that hand-writes `{"action": "approve", "edited_doc": "..."}` cannot catch
that class of drift: it just restates the Python side twice. So this test
*reads the payload out of the .tsx file* and drives the whole real chain with it:

    ProfileReflectionFlow
      → ABANDONED InteractionFlow child (real)
        → interactions row (real)
          → POST /api/interactions/{id}/resolve (the real route, real auth)
            → Temporal signal to the live child
              → post_resolve_activity="apply_profile_reflection" (resolved by NAME)
                → agent_personalities + agent_profile_revisions

Rename a key on EITHER side and the two halves stop meeting in the middle: the
applier falls back to `metadata.proposed_doc` (or does nothing at all) and the
assertions below fail.

Only `send_interaction_card` is stubbed — the one step that leaves the process.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.api.routes.interactions import get_workflow_client
from aegis.config import Settings
from aegis_worker.activities.interactions import InteractionActivities
from aegis_worker.activities.profile import ProfileActivities
from aegis_worker.flows.interaction import InteractionFlow
from aegis_worker.flows.profile_reflection import ProfileReflectionConfig, ProfileReflectionFlow
from httpx import ASGITransport, AsyncClient
from temporalio import activity
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

AGENT = "zza2e-profile"
PURPOSE = "profile_reflection"

REPO = Path(__file__).resolve().parents[2]
PANEL = REPO / "admin-panel" / "frontend" / "src" / "pages" / "InteractionDetail.tsx"

CURRENT_DOC = "Owner is based in Pune.\nPrefers concise answers.\n" + ("x" * 200)
PROPOSED_DOC = CURRENT_DOC + "\nRuns a homelab swarm called meem."


# ------------------------------------------------------- the payload, from .tsx


_CALL_RE = re.compile(r"submitPayload\(\{([^{}]*)\}\)")
_STRING_RE = re.compile(r"""^(['"])(.*)\1$""")


def _parse_object_literal(body: str) -> dict[str, tuple[str, str]]:
    """`action: 'approve', edited_doc: draft` → {key: (kind, value)}.

    `kind` is "literal" for a quoted string and "binding" for an identifier
    (whatever the user typed into the panel).
    """
    fields: dict[str, tuple[str, str]] = {}
    for part in body.split(","):
        if ":" not in part:
            continue
        key, _, value = part.partition(":")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        m = _STRING_RE.match(value)
        fields[key] = ("literal", m.group(2)) if m else ("binding", value)
    return fields


def _panel_payload(action: str) -> dict[str, str]:
    """The JSON body the draft_review panel POSTs for `action`, read from source.

    Literal fields keep the panel's own value; every free-text field gets a
    distinct sentinel derived from ITS OWN key, so the assertion "the profile
    holds a value the panel sent" holds no matter which key carries the
    document — and stops holding the moment the two sides disagree about it.
    """
    src = PANEL.read_text()
    for body in _CALL_RE.findall(src):
        fields = _parse_object_literal(body)
        if fields.get("action") == ("literal", action):
            # A regex that quietly matched nothing would make every assertion
            # below vacuous.
            assert len(fields) >= 2, f"panel payload for {action!r} has no data field: {fields}"
            assert any(k == "binding" for k, _ in fields.values()), (
                f"panel payload for {action!r} carries no user input: {fields}"
            )
            return {
                key: value if kind == "literal" else f"zza2e-ui[{key}] " + ("y" * 300)
                for key, (kind, value) in fields.items()
            }
    raise AssertionError(
        f"no submitPayload({{action: '{action}', …}}) call in {PANEL} — the draft_review "
        "panel is the only way a human can answer this card"
    )


def test_panel_still_has_both_draft_review_buttons():
    """Fails loudly if the panel is refactored away, rather than letting the
    end-to-end tests below fail with a confusing AssertionError."""
    approve = _panel_payload("approve")
    reject = _panel_payload("reject")
    assert approve["action"] == "approve"
    assert reject["action"] == "reject"
    assert approve != reject


# --------------------------------------------------------------------- fixtures

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
def settings():
    return Settings(**_TEST_REQUIRED_SETTINGS)


@pytest.fixture
def auth_headers():
    return {"Authorization": "Basic " + base64.b64encode(b"admin:admin").decode()}


async def _wipe(conn):
    await conn.execute("DELETE FROM agent_profile_revisions WHERE agent_id = $1", AGENT)
    await conn.execute("DELETE FROM agent_personalities WHERE agent_id = $1", AGENT)
    await conn.execute("DELETE FROM agent_memory WHERE agent_id = $1", AGENT)
    await conn.execute("DELETE FROM interactions WHERE agent_id = $1", AGENT)
    await conn.execute("DELETE FROM notification_log WHERE agent_id = $1", AGENT)
    await conn.execute("DELETE FROM llm_calls WHERE agent_id = $1", AGENT)


@pytest_asyncio.fixture(loop_scope="function")
async def clean_db(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, role, system_prompt_path, active) "
            "VALUES ($1, $1, 'assistant', 'personalities/x', TRUE) "
            "ON CONFLICT (id) DO NOTHING",
            AGENT,
        )
        await _wipe(conn)
        await conn.execute(
            "INSERT INTO agent_personalities (agent_id, kind, content) VALUES ($1,'user',$2) "
            "ON CONFLICT (agent_id, kind) DO UPDATE SET content = EXCLUDED.content",
            AGENT,
            CURRENT_DOC,
        )
        await conn.execute(
            "INSERT INTO agent_memory (agent_id, content, importance, source) "
            "VALUES ($1, 'Owner runs a homelab swarm called meem', 0.9, 'correction')",
            AGENT,
        )
    from aegis.services.personalities import invalidate

    invalidate(AGENT)
    yield db_pool
    async with db_pool.acquire() as conn:
        await _wipe(conn)
        await conn.execute("DELETE FROM agents WHERE id = $1", AGENT)


A5_CLAIM = "The owner does not take meetings in the morning."
A5_TRIPLE = [
    "Owner refuses meetings before 11am",
    "Owner moved the standup because it was before 11am",
    "Owner asked never to be booked in the morning",
]


class FakeLLM:
    """A5: the flow makes two calls. The generalisation one answers with the ids
    it can SEE in its own prompt — so if the pool query stopped excluding
    already-promoted rows, this fake would name them again and the idempotence
    test below would fail."""

    async def think(self, **kwargs):
        if kwargs.get("purpose") == "profile_generalization":
            ids = [int(m) for m in re.findall(r'"id":\s*(\d+)', kwargs.get("prompt") or "")]
            body = (
                [{"claim": A5_CLAIM, "supporting_memory_ids": ids, "confidence": 0.9}]
                if len(ids) >= 3
                else []
            )
        else:
            body = {
                "proposed_doc": PROPOSED_DOC,
                "rationale": "the owner mentioned a homelab twice",
                "changed_lines": ["+ Runs a homelab swarm called meem."],
            }
        return {
            "response": json.dumps(body),
            "model": "fake-balanced",
            "prompt_tokens": 5,
            "completion_tokens": 7,
        }


def _stub_card():
    @activity.defn(name="send_interaction_card")
    async def send_interaction_card(
        interaction_id, agent_id, kind, prompt, options, allow_hint=False
    ):
        return {"ok": True, "delivery_ref": {"adapter": "web"}}

    return send_interaction_card


@asynccontextmanager
async def _flow_worker(client, pool):
    prof = ProfileActivities(db_pool=pool, llm_client=FakeLLM())
    inter = InteractionActivities(pool)
    task_queue = f"tq-{uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[ProfileReflectionFlow, InteractionFlow],
        activities=[
            prof.check_profile_budget,
            prof.read_profile_context,
            prof.gather_profile_evidence,
            prof.propose_generalizations,
            prof.propose_profile_patch,
            prof.record_profile_card,
            prof.apply_profile_reflection,
            inter.insert_interaction,
            inter.resolve_interaction,
            inter.apply_interaction_timeout,
            inter.update_interaction_delivery_ref,
            _stub_card(),
        ],
    ):

        async def run():
            return await client.execute_workflow(
                ProfileReflectionFlow.run,
                ProfileReflectionConfig(agent_id=AGENT, aegis_ui_url="https://aegis.example.com"),
                id=f"profile-e2e-{uuid4().hex[:8]}",
                task_queue=task_queue,
            )

        yield run


def _app(pool, settings, temporal_client):
    app = create_app(run_lifespan=False)
    app.state.db_pool = pool
    app.dependency_overrides[get_settings] = lambda: settings
    # The REAL route, signalling the REAL (abandoned) InteractionFlow child on
    # the test server — not a mocked handle.
    app.dependency_overrides[get_workflow_client] = lambda: temporal_client
    return app


async def _wait_for(coro_factory, timeout: float = 15.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = await coro_factory()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            return value
        await asyncio.sleep(0.05)


async def _card_row(pool):
    return await pool.fetchrow(
        "SELECT id, kind, metadata FROM interactions WHERE origin = $1 AND agent_id = $2",
        PURPOSE,
        AGENT,
    )


async def _doc(pool) -> str:
    return (
        await pool.fetchval(
            "SELECT content FROM agent_personalities WHERE agent_id = $1 AND kind = 'user'", AGENT
        )
        or ""
    )


async def _revisions(pool):
    return await pool.fetch(
        "SELECT source, interaction_id, before_content, after_content "
        "FROM agent_profile_revisions WHERE agent_id = $1 ORDER BY id",
        AGENT,
    )


async def _resolve(app, auth_headers, interaction_id, payload):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            f"/api/interactions/{interaction_id}/resolve",
            json={"response": payload},
            headers=auth_headers,
        )


# ------------------------------------------------------------------------ tests


@pytest.mark.asyncio
async def test_the_panels_approve_payload_writes_the_profile(clean_db, settings, auth_headers):
    """The guard. Everything the admin panel Approve button submits, end to end.

    The document that lands must be the one the PANEL sent, not the
    `proposed_doc` the card was built from — that fallback is exactly what a
    key-name drift would silently degrade to.
    """
    payload = _panel_payload("approve")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        # The card's 7-day archive timer would fire instantly under automatic
        # time skipping, archiving the interaction before the POST lands.
        with env.auto_time_skipping_disabled():
            async with _flow_worker(env.client, clean_db) as run:
                flow = await run()
                assert flow["status"] == "carded", flow
                row = await _wait_for(lambda: _card_row(clean_db))
                assert row is not None, "the flow never created a draft_review card"

                # The panel initialises its editor from `metadata.proposed_doc`
                # on THIS route — a response that omits `metadata` leaves the
                # editor blank.
                async with AsyncClient(
                    transport=ASGITransport(app=_app(clean_db, settings, env.client)),
                    base_url="http://test",
                ) as client:
                    got = await client.get(
                        f"/api/interactions/{row['id']}", headers=auth_headers
                    )
                assert got.status_code == 200
                assert got.json()["metadata"]["proposed_doc"] == PROPOSED_DOC

                resp = await _resolve(
                    _app(clean_db, settings, env.client), auth_headers, row["id"], payload
                )
                assert resp.status_code == 200, resp.text
                assert resp.json()["already_resolved"] is False

                # The child is ABANDONED — wait for IT, not the parent, or the
                # worker shuts down mid post-resolve.
                child = await env.client.get_workflow_handle(flow["child_id"]).result()

                final_doc = await _doc(clean_db)
                revisions = await _revisions(clean_db)

    assert child["status"] == "resolved"

    # The value written came out of the PANEL's payload, and is not the
    # proposed_doc fallback. Both halves matter: the second alone would pass
    # with the applier ignoring `edited_doc` entirely if the panel happened to
    # send the proposed text.
    assert final_doc in set(payload.values()), (
        f"the applied document is not one the panel submitted "
        f"(panel keys: {sorted(payload)}, applied {len(final_doc)} chars)"
    )
    assert final_doc != PROPOSED_DOC, "the applier fell back to proposed_doc"
    assert final_doc != CURRENT_DOC, "nothing was written"

    assert len(revisions) == 1
    assert revisions[0]["source"] == PURPOSE
    assert str(revisions[0]["interaction_id"]) == str(row["id"])
    assert revisions[0]["before_content"] == CURRENT_DOC
    assert revisions[0]["after_content"] == final_doc


@pytest.mark.asyncio
async def test_the_panels_reject_payload_writes_nothing(clean_db, settings, auth_headers):
    """The same chain, the Reject button: the profile is untouched, no revision
    is logged — and the reason the human typed is still banked as a lesson by
    the resolve route's `record_correction_from_interaction`."""
    payload = _panel_payload("reject")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            async with _flow_worker(env.client, clean_db) as run:
                flow = await run()
                row = await _wait_for(lambda: _card_row(clean_db))
                assert row is not None

                resp = await _resolve(
                    _app(clean_db, settings, env.client), auth_headers, row["id"], payload
                )
                assert resp.status_code == 200, resp.text

                await env.client.get_workflow_handle(flow["child_id"]).result()

                final_doc = await _doc(clean_db)
                revisions = await _revisions(clean_db)
                lessons = await clean_db.fetch(
                    "SELECT content FROM agent_memory WHERE agent_id = $1 "
                    "AND source = 'interaction_correction'",
                    AGENT,
                )

    assert final_doc == CURRENT_DOC, "a rejected draft was written to the profile"
    assert revisions == []
    # The reject reason is the one thing a rejection should leave behind.
    assert len(lessons) == 1
    assert any(str(v) in lessons[0]["content"] for v in payload.values() if v != "reject")


# ----------------------------------------------------------------------- A5


async def _seed_triple(pool) -> list[int]:
    ids = []
    for content in A5_TRIPLE:
        ids.append(
            await pool.fetchval(
                "INSERT INTO agent_memory (agent_id, content, importance, source) "
                "VALUES ($1, $2, 0.7, 'correction') RETURNING id",
                AGENT,
                content,
            )
        )
    return sorted(ids)


async def _memory_rows(pool):
    return [
        dict(r)
        for r in await pool.fetch(
            "SELECT id, content, importance, source FROM agent_memory "
            "WHERE agent_id = $1 ORDER BY id",
            AGENT,
        )
    ]


@pytest.mark.asyncio
async def test_an_approved_promotion_does_not_come_back_next_week(
    clean_db, settings, auth_headers
):
    """A5 acceptance, driven end to end by rows the REAL chain wrote.

    Nothing here is hand-seeded on the suppression side: the `interactions`
    metadata comes from the flow, and the `agent_profile_revisions` row that
    proves the write happened comes from `apply_profile_reflection` firing on
    the panel's Approve payload. A second `propose_generalizations` pass over
    the same data then has an empty pool and returns nothing.

    And the memories themselves are untouched — promotion is recorded against
    the decision that authorised it, not by mutating the owner's memory rows.
    """
    payload = _panel_payload("approve")
    ids = await _seed_triple(clean_db)
    before = await _memory_rows(clean_db)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            async with _flow_worker(env.client, clean_db) as run:
                flow = await run()
                assert flow["status"] == "carded", flow
                assert flow["generalizations"] == 1
                row = await _wait_for(lambda: _card_row(clean_db))
                assert row is not None

                metadata = row["metadata"]
                metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
                assert set(ids).issubset(set(metadata["promoted_memory_ids"]))

                resp = await _resolve(
                    _app(clean_db, settings, env.client), auth_headers, row["id"], payload
                )
                assert resp.status_code == 200, resp.text
                child = await env.client.get_workflow_handle(flow["child_id"]).result()

    assert child["status"] == "resolved"
    revisions = await _revisions(clean_db)
    assert len(revisions) == 1, "nothing was written, so nothing was promoted"

    # The second week. Same activity, same data, same fake model.
    acts = ProfileActivities(db_pool=clean_db, llm_client=FakeLLM())
    again = await ActivityEnvironment().run(acts.propose_generalizations, AGENT)

    assert again["candidates"] == []
    assert again["pool"] == 0
    assert again["excluded"] == len(before)
    assert await _memory_rows(clean_db) == before, "promotion mutated the owner's memories"


@pytest.mark.asyncio
async def test_a_rejected_promotion_comes_back(clean_db, settings, auth_headers):
    """The other side of the same gate: Reject writes no revision, so the rows
    stay in the pool and the claim is still available to propose. Without this,
    the test above would also pass with `_promoted_memory_ids` excluding every
    row that was ever carded, approved or not."""
    payload = _panel_payload("reject")
    await _seed_triple(clean_db)
    before = await _memory_rows(clean_db)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            async with _flow_worker(env.client, clean_db) as run:
                flow = await run()
                row = await _wait_for(lambda: _card_row(clean_db))
                resp = await _resolve(
                    _app(clean_db, settings, env.client), auth_headers, row["id"], payload
                )
                assert resp.status_code == 200, resp.text
                await env.client.get_workflow_handle(flow["child_id"]).result()

    assert await _revisions(clean_db) == []

    acts = ProfileActivities(db_pool=clean_db, llm_client=FakeLLM())
    again = await ActivityEnvironment().run(acts.propose_generalizations, AGENT)

    assert again["excluded"] == 0
    assert len(again["candidates"]) == 1
    # The reject reason was banked as a memory, so the pool grew rather than
    # shrank — but every row that was there before is still there.
    assert (await _memory_rows(clean_db))[: len(before)] == before
