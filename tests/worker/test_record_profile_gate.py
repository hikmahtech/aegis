"""While the record is on, the weekly persona draft stands down before it
spends a model call, and an old draft card applies nothing (vault record
spec §5, "Until §7 ships")."""

from __future__ import annotations

import pytest_asyncio
from aegis.db import run_migrations
from aegis.services import personalities as p
from aegis.services import vault_layout as vl
from aegis_worker.activities.profile import ProfileActivities
from temporalio.testing import ActivityEnvironment

AGENT = "zzrec-gate"


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, active) VALUES ($1, 'Z', 'r', '', true)", AGENT
    )
    await vl.save_layout(db_pool, {"record": {"enabled": True}})
    yield db_pool
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)
    await db_pool.execute("DELETE FROM settings WHERE key = 'vault_layout'")
    vl.invalidate_cache()
    p.invalidate()


class _NoLLM:
    async def think(self, **_kw):  # pragma: no cover — must never be reached
        raise AssertionError("no model call while the record is on")


async def test_the_budget_gate_says_record_in_vault(pool):
    acts = ProfileActivities(db_pool=pool, llm_client=_NoLLM())
    gate = await ActivityEnvironment().run(acts.check_profile_budget, AGENT, 1)
    assert gate["allow"] is False and gate["reason"] == "record_in_vault"


async def test_an_old_draft_card_applies_nothing(pool):
    await p.set_personality(pool, AGENT, {"user": "compiled from the vault"})
    acts = ProfileActivities(db_pool=pool)
    res = await ActivityEnvironment().run(
        acts.apply_profile_reflection,
        "00000000-0000-0000-0000-000000000001",
        {"action": "approve"},
        {"agent_id": AGENT, "kind": "user", "proposed_doc": "a rewrite"},
    )
    assert res == {"applied": False, "status": "record_in_vault"}
    assert (await p.get_personality(pool, AGENT, use_cache=False))["user"] == "compiled from the vault"
