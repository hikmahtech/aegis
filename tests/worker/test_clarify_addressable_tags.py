"""Issue #36 PR 3 — clarify addressable routing, classifier assignee vocabulary,
and context pre-fetch are DERIVED from active agents (mention_aliases +
behavior tags), not hardcoded ids. Regression + custom-agent coverage.

Uses the real db_pool. A custom agent is inserted as `tagtest-ops` and removed
in finally; the module-level registry cache is reset around each mutation so
the fresh rows are read.
"""

from __future__ import annotations

import pytest
from aegis_worker.activities import clarify as clarify_mod
from aegis_worker.activities.clarify import (
    ClarifyActivities,
    _addressable_agents,
    _assignee_labels,
    get_agent_registry,
)


def _reset_reg_cache():
    clarify_mod._agent_reg_cache.update(reg=None, ts=0.0)


def _task(labels: list[str]) -> dict:
    return {
        "id": "task-addr",
        "content": "Title here",
        "description": "desc",
        "labels": labels,
        "source_tag": "#manual",
        "latest_user_note": "Please look at this.",
        "last_note_at": None,
    }


# ── Pure derivation helpers (no DB) ──


def test_addressable_orders_gtd_owner_first():
    reg = {
        "maou": {"aliases": ["@maou"], "caps": {"finance"}},
        "sebas": {"aliases": ["@sebas"], "caps": {"gtd"}},
        "raphael": {"aliases": ["@raphael"], "caps": {"research"}},
    }
    pairs = _addressable_agents(reg)
    # gtd owner (sebas) first → wins co-occurrence, preserving the old guarantee.
    assert pairs[0] == ("@sebas", "sebas_followup")
    assert ("@maou", "maou_followup") in pairs
    assert ("@raphael", "raphael_followup") in pairs


def test_assignee_labels_include_me_and_all_aliases():
    reg = {
        "sebas": {"aliases": ["@sebas"], "caps": {"gtd"}},
        "pandoras-actor": {"aliases": ["@pandora"], "caps": {"infra"}},
    }
    labels = _assignee_labels(reg)
    assert labels[0] == "@me"
    assert "@sebas" in labels and "@pandora" in labels


@pytest.mark.asyncio
async def test_registry_falls_back_to_defaults_without_pool():
    # No pool → shipped 4-agent defaults, so routing never breaks.
    reg = await get_agent_registry(None)
    assert reg["sebas"]["caps"] == {"gtd"}
    assert reg["pandoras-actor"]["aliases"] == ["@pandora"]


# ── DB-backed: seed agents preserved, custom agent routed ──


@pytest.mark.asyncio
async def test_seed_agent_followup_short_circuit(db_pool):
    """A @raphael-labelled task with a fresh user comment routes to
    raphael_followup (today's behavior, derived from the DB)."""
    _reset_reg_cache()
    acts = ClarifyActivities(db_pool=db_pool)
    out = await acts.classify_one(_task(["@raphael", "#manual"]))
    assert out["classification"] == "raphael_followup"
    assert out["assignee"] == "@raphael"


@pytest.mark.asyncio
async def test_custom_agent_alias_routes_and_prefetches_by_tag(db_pool):
    """A custom research agent addressed by its mention alias routes to
    <id>_followup AND gets the knowledge pre-fetch (gated on the `research`
    tag, not the literal id 'raphael')."""
    await db_pool.execute(
        """
        INSERT INTO agents (id, name, role, system_prompt_path, capabilities,
                            model_tier, metadata, active)
        VALUES ('tagtest-ops', 'tagtest-ops', 'test', '', '["research"]'::jsonb,
                'balanced', '{"mention_aliases": ["ops"]}'::jsonb, TRUE)
        ON CONFLICT (id) DO NOTHING
        """
    )
    _reset_reg_cache()
    try:
        acts = ClarifyActivities(db_pool=db_pool)
        # classify: @ops label routes to the custom agent's followup branch.
        out = await acts.classify_one(_task(["@ops", "#manual"]))
        assert out["classification"] == "tagtest-ops_followup"
        assert out["assignee"] == "@ops"

        # apply_outcome: research-tagged target gets the KS pre-fetch hook.
        from unittest.mock import patch

        async def fake_ks(synthetic, task):
            return synthetic + "\n\nExisting knowledge:\n- note\n"

        with patch.object(ClarifyActivities, "_maybe_attach_ks_context", side_effect=fake_ks):
            outcome = await acts.apply_outcome(_task(["@ops", "#manual"]), out)
        payload = outcome["interaction_payload"]
        assert payload["target_agent"] == "tagtest-ops"
        assert "Existing knowledge:" in payload["synthetic_input"]
    finally:
        await db_pool.execute("DELETE FROM agents WHERE id = 'tagtest-ops'")
        _reset_reg_cache()


@pytest.mark.asyncio
async def test_classify_prompt_lists_derived_assignees(db_pool):
    """The classifier prompt's assignee vocabulary is built from the active
    agents' aliases (so a custom label is assignable)."""
    await db_pool.execute(
        """
        INSERT INTO agents (id, name, role, system_prompt_path, capabilities,
                            model_tier, metadata, active)
        VALUES ('tagtest-ops', 'tagtest-ops', 'test', '', '["research"]'::jsonb,
                'balanced', '{"mention_aliases": ["ops"]}'::jsonb, TRUE)
        ON CONFLICT (id) DO NOTHING
        """
    )
    _reset_reg_cache()
    try:
        reg = await get_agent_registry(db_pool)
        acts = ClarifyActivities(db_pool=db_pool)
        from aegis_worker.activities.clarify import _RULES

        prompt = acts._build_classify_prompt(_task(["#manual"]), _RULES, _assignee_labels(reg))
        assert "@ops" in prompt and "@me" in prompt and "@sebas" in prompt
    finally:
        await db_pool.execute("DELETE FROM agents WHERE id = 'tagtest-ops'")
        _reset_reg_cache()
