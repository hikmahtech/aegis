"""apply_profile_patch / list_profile_revisions / revert_profile_revision.

The auditable persona write path (migration 015). Real Postgres (:25432) via
the db_pool fixture — agent_profile_revisions rows cascade with the agent.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from aegis.db import run_migrations
from aegis.services import personalities as p

AGENT = "zzprofile-rev"


@pytest_asyncio.fixture(loop_scope="function")
async def agent_pool(db_pool):
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, active) "
        "VALUES ($1, 'Z', 'r', '', true)",
        AGENT,
    )
    p.invalidate()
    yield db_pool
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)
    p.invalidate()


async def _revision_count(pool) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM agent_profile_revisions WHERE agent_id = $1", AGENT
    )


async def test_patch_writes_exactly_one_revision_with_prior_content(agent_pool):
    await p.set_personality(agent_pool, AGENT, {"user": "prior user context"})

    result = await p.apply_profile_patch(
        agent_pool,
        AGENT,
        "user",
        "prior user context, plus a freshly learned fact",
        source="unit-test",
    )

    rows = await agent_pool.fetch(
        "SELECT * FROM agent_profile_revisions WHERE agent_id = $1", AGENT
    )
    assert len(rows) == 1
    assert rows[0]["before_content"] == "prior user context"
    assert rows[0]["after_content"] == "prior user context, plus a freshly learned fact"
    assert rows[0]["kind"] == "user"
    assert rows[0]["source"] == "unit-test"
    assert rows[0]["interaction_id"] is None
    assert result["revision_id"] == rows[0]["id"]

    fresh = await p.get_personality(agent_pool, AGENT, use_cache=False)
    assert fresh["user"] == "prior user context, plus a freshly learned fact"


async def test_patch_from_empty_is_allowed_and_logs_empty_before(agent_pool):
    """The shrink guard must not fire when there is no prior content."""
    result = await p.apply_profile_patch(agent_pool, AGENT, "user", "x", source="seed")
    assert result["before_length"] == 0
    before = await agent_pool.fetchval(
        "SELECT before_content FROM agent_profile_revisions WHERE id = $1",
        result["revision_id"],
    )
    assert before == ""


async def test_shrink_beyond_half_refused_and_leaves_both_tables_untouched(agent_pool):
    original = "A" * 100
    await p.set_personality(agent_pool, AGENT, {"user": original})
    before_count = await _revision_count(agent_pool)

    with pytest.raises(ValueError, match="refusing to shrink"):
        await p.apply_profile_patch(agent_pool, AGENT, "user", "A" * 49, source="bad-writer")

    assert await _revision_count(agent_pool) == before_count
    assert (await p.get_personality(agent_pool, AGENT, use_cache=False))["user"] == original


async def test_shrink_to_exactly_half_is_allowed(agent_pool):
    await p.set_personality(agent_pool, AGENT, {"user": "A" * 100})
    await p.apply_profile_patch(agent_pool, AGENT, "user", "A" * 50, source="trim")
    assert (await p.get_personality(agent_pool, AGENT, use_cache=False))["user"] == "A" * 50


async def test_shrink_allowed_with_explicit_flag(agent_pool):
    await p.set_personality(agent_pool, AGENT, {"user": "A" * 100})
    await p.apply_profile_patch(
        agent_pool, AGENT, "user", "tiny", source="operator", allow_shrink=True
    )
    assert (await p.get_personality(agent_pool, AGENT, use_cache=False))["user"] == "tiny"


async def test_patch_rejects_unknown_kind(agent_pool):
    with pytest.raises(ValueError, match="unknown personality kind"):
        await p.apply_profile_patch(agent_pool, AGENT, "vibe", "x", source="unit-test")
    assert await _revision_count(agent_pool) == 0


async def test_patch_records_interaction_id(agent_pool):
    interaction_id = "11111111-2222-3333-4444-555555555555"
    result = await p.apply_profile_patch(
        agent_pool,
        AGENT,
        "user",
        "approved by a human",
        source="interaction",
        interaction_id=interaction_id,
    )
    stored = await agent_pool.fetchval(
        "SELECT interaction_id FROM agent_profile_revisions WHERE id = $1",
        result["revision_id"],
    )
    assert str(stored) == interaction_id


async def test_list_revisions_newest_first_and_kind_filter(agent_pool):
    await p.apply_profile_patch(agent_pool, AGENT, "user", "u1", source="s1")
    await p.apply_profile_patch(agent_pool, AGENT, "user", "u1 grown longer", source="s2")
    await p.apply_profile_patch(agent_pool, AGENT, "memory", "m1", source="s3")

    all_revs = await p.list_profile_revisions(agent_pool, AGENT)
    assert [r["source"] for r in all_revs] == ["s3", "s2", "s1"]

    user_only = await p.list_profile_revisions(agent_pool, AGENT, kind="user")
    assert [r["source"] for r in user_only] == ["s2", "s1"]

    assert len(await p.list_profile_revisions(agent_pool, AGENT, limit=1)) == 1
    with pytest.raises(ValueError, match="unknown personality kind"):
        await p.list_profile_revisions(agent_pool, AGENT, kind="vibe")


async def test_revert_restores_byte_identical_content_and_logs_a_revision(agent_pool):
    original = "line one\n  line two with trailing space \nline three\n"
    await p.set_personality(agent_pool, AGENT, {"user": original})

    patched = await p.apply_profile_patch(
        agent_pool, AGENT, "user", original + "an automated addition", source="flow"
    )
    count_after_patch = await _revision_count(agent_pool)

    revert = await p.revert_profile_revision(agent_pool, patched["revision_id"])

    assert (await p.get_personality(agent_pool, AGENT, use_cache=False))["user"] == original
    assert await _revision_count(agent_pool) == count_after_patch + 1
    row = await agent_pool.fetchrow(
        "SELECT * FROM agent_profile_revisions WHERE id = $1", revert["revision_id"]
    )
    assert row["source"] == "revert"
    assert row["after_content"] == original


async def test_revert_of_a_growth_patch_bypasses_the_shrink_guard(agent_pool):
    """Undoing an edit that tripled the doc is a >50% shrink by definition."""
    await p.set_personality(agent_pool, AGENT, {"user": "short"})
    patched = await p.apply_profile_patch(agent_pool, AGENT, "user", "x" * 300, source="flow")

    await p.revert_profile_revision(agent_pool, patched["revision_id"])
    assert (await p.get_personality(agent_pool, AGENT, use_cache=False))["user"] == "short"


async def test_revert_unknown_id_raises(agent_pool):
    with pytest.raises(ValueError, match="unknown profile revision id"):
        await p.revert_profile_revision(agent_pool, 987654321)
