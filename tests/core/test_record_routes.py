"""The record's guards on the admin API (vault record spec §5): the switch,
the read-only `user` document, the revert refusal, and what the Vault page
reads. Made-up agents and notes."""

from __future__ import annotations

import base64

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.db import run_migrations
from aegis.services import personalities as p
from aegis.services import vault_layout as vl
from httpx import ASGITransport, AsyncClient

from tests.notes_vault import device_commit, make_vault, needs_git

AGENT = "zzrec-route"
URL = "/api/admin/notes/layout"
AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:admin").decode()}
BASE = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
}


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, active) VALUES ($1, 'Z', 'r', '', true)",
        AGENT,
    )
    await db_pool.execute("DELETE FROM settings WHERE key IN ('vault_layout', 'notes_record_state')")
    vl.invalidate_cache()
    p.invalidate()
    yield db_pool
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)
    await db_pool.execute("DELETE FROM settings WHERE key IN ('vault_layout', 'notes_record_state')")
    vl.invalidate_cache()
    p.invalidate()


def _client(pool, settings):
    app = create_app(run_lifespan=False)
    app.state.db_pool = pool
    app.dependency_overrides[get_settings] = lambda: settings
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _vault_settings(v) -> Settings:
    return Settings(**BASE, notes_repo_url=str(v["remote"]), notes_path=str(v["cfg"].path),
                    gmail_token_dir=str(v["tmp"]))


async def _gtd_holder(pool) -> str:
    from aegis.services.agents import resolve_tag
    return await resolve_tag(pool, "gtd")


async def test_the_personality_put_answers_409_only_when_it_would_change_user(pool):
    await p.set_personality(pool, AGENT, {"user": "compiled", "soul": "s"})
    async with _client(pool, Settings(**BASE)) as c:
        ok = await c.put(f"/api/admin/agents/{AGENT}/personality", headers=AUTH, json={"user": "hand"})
        assert ok.status_code == 200
        await vl.save_layout(pool, {"record": {"enabled": True}})
        refused = await c.put(f"/api/admin/agents/{AGENT}/personality", headers=AUTH, json={"user": "hand 2"})
        assert refused.status_code == 409 and "vault" in refused.json()["detail"]
        same = await c.put(f"/api/admin/agents/{AGENT}/personality", headers=AUTH,
                           json={"user": "hand", "soul": "new soul"})
        assert same.status_code == 200 and same.json()["soul"] == "new soul"


async def test_a_user_revision_cannot_be_reverted_while_the_record_is_on(pool):
    await p.apply_profile_patch(pool, AGENT, "user", "v1", source="test")
    rev = await p.apply_profile_patch(pool, AGENT, "user", "v2", source="test")
    await vl.save_layout(pool, {"record": {"enabled": True}})
    with pytest.raises(p.RecordInVault):
        await p.revert_profile_revision(pool, rev["revision_id"])
    soul = await p.apply_profile_patch(pool, AGENT, "soul", "s2", source="test")
    await p.revert_profile_revision(pool, soul["revision_id"])  # other kinds are not the record's


@needs_git
async def test_turning_the_record_on_is_refused_until_a_draft_is_accepted(pool, tmp_path):
    v = make_vault(tmp_path, {"me/about.draft.md": "# About\n- draft\n"})
    holder = await _gtd_holder(pool)
    before = (await p.get_personality(pool, holder, use_cache=False))["user"]
    await p.set_personality(pool, holder, {"user": "The one real document."})
    try:
        async with _client(pool, _vault_settings(v)) as c:
            refused = await c.put(URL, headers=AUTH, json={"record": {"enabled": True}})
            assert refused.status_code == 400
            assert refused.json()["detail"].startswith(f"record.enabled: {holder}'s user document")
            assert (await c.get(URL, headers=AUTH)).json()["layout"]["record"]["enabled"] is False
            device_commit(v, {"me/about.md": "# About\n- accepted\n"})
            ok = await c.put(URL, headers=AUTH, json={"record": {"enabled": True}})
            assert ok.status_code == 200 and ok.json()["layout"]["record"]["enabled"] is True
    finally:
        await p.set_personality(pool, holder, {"user": before})


@needs_git
async def test_the_vault_page_reads_the_state_and_the_waiting_drafts(pool, tmp_path):
    from aegis.services import notes
    from aegis.services.settings_store import put_setting

    v = make_vault(tmp_path, {"me/about.draft.md": "# About\n", "me/money.draft.md": "# Money\n"})
    notes.read_record_sync(v["cfg"], vl.DEFAULT_LAYOUT)  # the shared checkout exists, as in production
    await put_setting(pool, "notes_record_state", {"commit": "abc", "agents": {AGENT: {"chars": 10}}})
    async with _client(pool, _vault_settings(v)) as c:
        body = (await c.get(URL, headers=AUTH)).json()
    assert body["drafts"] == ["me/about.draft.md", "me/money.draft.md"]
    assert body["record_state"]["agents"][AGENT]["chars"] == 10
    assert "gtd" in body["options"]["tags"]


async def test_the_page_lists_no_drafts_without_a_vault(pool):
    async with _client(pool, Settings(**BASE)) as c:
        body = (await c.get(URL, headers=AUTH)).json()
    assert body["drafts"] == [] and body["record_state"] == {}


async def test_the_seed_button_starts_the_flow_as_the_gtd_holder(pool):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    client = MagicMock()
    client.start_workflow = AsyncMock(return_value=SimpleNamespace(id="manual-record_seed-1"))
    app = create_app(run_lifespan=False)
    app.state.db_pool = pool
    app.state.temporal_client = client
    app.dependency_overrides[get_settings] = lambda: Settings(**BASE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/admin/notes/record/seed", headers=AUTH)
    assert r.status_code == 200 and r.json()["workflow_id"] == "manual-record_seed-1"
    args, kwargs = client.start_workflow.call_args
    assert args[0] == "RecordSeedFlow" and args[1] == {"agent_id": await _gtd_holder(pool)}
    assert kwargs["task_queue"] == "aegis-main"
