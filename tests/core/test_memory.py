"""Tests for the per-agent memory learning loop (Phase 4)."""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio
from aegis.services.memory import (
    all_memories,
    apply_consolidation,
    format_memories,
    prune_memories,
    recent_memories,
    record_correction_from_interaction,
    record_gmail_triage_correction,
    record_memory,
)

_AID = "zzmem-agent"


@pytest_asyncio.fixture(loop_scope="function")
async def mem_agent(db_pool):
    await db_pool.execute("DELETE FROM agent_memory WHERE agent_id = $1", _AID)
    await db_pool.execute("DELETE FROM agents WHERE id = $1", _AID)
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, active) "
        "VALUES ($1, 'Z', 'tester', '', true)",
        _AID,
    )
    yield db_pool
    await db_pool.execute("DELETE FROM agent_memory WHERE agent_id = $1", _AID)
    await db_pool.execute("DELETE FROM agents WHERE id = $1", _AID)


async def test_record_and_recent(mem_agent):
    await record_memory(mem_agent, _AID, "low note", importance=0.2)
    await record_memory(mem_agent, _AID, "high lesson", importance=0.9)
    out = await recent_memories(mem_agent, _AID, limit=5)
    assert out[0] == "high lesson"  # importance-ordered
    assert "low note" in out


async def test_record_empty_is_noop(mem_agent):
    await record_memory(mem_agent, _AID, "   ")
    assert await recent_memories(mem_agent, _AID) == []


async def test_correction_from_interaction_records_with_reason(mem_agent):
    await record_correction_from_interaction(
        mem_agent, _AID, "Open a PR on the alert?", {"value": "reject", "reason": "wrong base branch"}
    )
    out = await recent_memories(mem_agent, _AID)
    assert len(out) == 1
    assert "wrong base branch" in out[0] and "reject" in out[0]


async def test_correction_without_reason_is_noop(mem_agent):
    await record_correction_from_interaction(mem_agent, _AID, "Approve?", {"value": "accept"})
    assert await recent_memories(mem_agent, _AID) == []


async def test_prune_caps_to_keep(mem_agent):
    for i in range(10):
        await record_memory(mem_agent, _AID, f"mem {i}", importance=0.5)
    deleted = await prune_memories(mem_agent, _AID, keep=3)
    assert deleted == 7
    assert len(await recent_memories(mem_agent, _AID, limit=50)) == 3


async def test_record_gmail_triage_correction_writes_and_dedupes(mem_agent):
    """(#116) A Gmail triage correction writes an agent_memory row; a second
    call for the SAME email_id is a no-op (idempotent)."""
    wrote = await record_gmail_triage_correction(
        mem_agent, _AID, "msg-123", "Newsletter roundup", "useless", "important"
    )
    assert wrote is True
    out = await recent_memories(mem_agent, _AID)
    assert len(out) == 1
    assert "Newsletter roundup" in out[0]
    assert "predicted useless" in out[0]
    assert "actually important" in out[0]

    wrote_again = await record_gmail_triage_correction(
        mem_agent, _AID, "msg-123", "Newsletter roundup", "useless", "important"
    )
    assert wrote_again is False
    assert len(await recent_memories(mem_agent, _AID)) == 1


async def test_record_gmail_triage_correction_distinct_ids_both_write(mem_agent):
    await record_gmail_triage_correction(mem_agent, _AID, "msg-a", "A", "useless", "important")
    await record_gmail_triage_correction(mem_agent, _AID, "msg-b", "B", "useless", "important")
    assert len(await recent_memories(mem_agent, _AID)) == 2


async def test_all_memories_returns_ids_and_metadata(mem_agent):
    """Consolidation needs the ids `recent_memories` throws away — that read
    path returns bare strings, so it cannot drive an op list."""
    await record_memory(mem_agent, _AID, "low note", importance=0.2, source="correction")
    await record_memory(mem_agent, _AID, "high lesson", importance=0.9, source="gmail_triage_correction")

    rows = await all_memories(mem_agent, _AID)

    assert [r["content"] for r in rows] == ["high lesson", "low note"]  # importance-ordered
    assert all(isinstance(r["id"], int) and r["id"] > 0 for r in rows)
    assert rows[0]["source"] == "gmail_triage_correction"
    assert rows[0]["importance"] == pytest.approx(0.9)
    assert rows[0]["created_at"] is not None
    assert len({r["id"] for r in rows}) == 2


def test_format_memories():
    assert format_memories([]) == ""
    s = format_memories(["a", "b"])
    assert "What you've learned" in s and "- a" in s and "- b" in s


# ------------------------------------------------------------------ #389
# `record_memory` runs inside `InteractionFlow`'s post-resolve hook, which
# Temporal retries (`maximum_attempts=2`). The books half of that hook is
# retry-idempotent; the memory write was not, so a hook that burned its 240s
# budget mid-flight came back on attempt 2 and wrote the belief twice.
#
# The fix is a partial UNIQUE index, not a SELECT-then-INSERT: the
# check-then-write race is exactly what a retry reproduces.


async def test_a_duplicate_live_memory_is_rejected_by_the_database(mem_agent):
    """The index must BITE, not merely exist.

    Asserted through a raw INSERT rather than `record_memory`, because
    `record_memory` swallows the conflict by design — running it twice proves
    only that the code path is quiet. This is the assertion that fails if the
    migration is reverted, the index is created non-unique, or its predicate
    stops matching the write.
    """
    await record_memory(mem_agent, _AID, "the owner banks with HDFC")
    with pytest.raises(asyncpg.UniqueViolationError):
        await mem_agent.execute(
            "INSERT INTO agent_memory (agent_id, content, importance, source) "
            "VALUES ($1, $2, 0.5, 'correction')",
            _AID,
            "the owner banks with HDFC",
        )


async def test_record_memory_writes_once_when_the_hook_is_retried(mem_agent):
    """Attempt 2 of a retried post-resolve hook re-runs `record_memory` with
    byte-identical content. One belief, one row."""
    for _ in range(3):
        await record_memory(mem_agent, _AID, "the owner answered: my grocer", importance=0.8)
    assert (
        await mem_agent.fetchval("SELECT count(*) FROM agent_memory WHERE agent_id = $1", _AID) == 1
    )
    assert await recent_memories(mem_agent, _AID) == ["the owner answered: my grocer"]


async def test_a_superseded_memory_can_be_learned_again(mem_agent):
    """The index is PARTIAL on `superseded_at IS NULL` on purpose.

    Consolidation soft-retires rather than deletes (migration 020), so a
    withdrawn belief must be re-learnable. A plain unique index would make the
    retirement permanent — a behaviour change wearing a bug fix's clothes.
    """
    await record_memory(mem_agent, _AID, "the owner prefers PRs against main")
    await mem_agent.execute(
        "UPDATE agent_memory SET superseded_at = now() WHERE agent_id = $1", _AID
    )

    await record_memory(mem_agent, _AID, "the owner prefers PRs against main")

    counts = await mem_agent.fetchrow(
        "SELECT count(*) AS total, count(*) FILTER (WHERE superseded_at IS NULL) AS live "
        "FROM agent_memory WHERE agent_id = $1",
        _AID,
    )
    assert (counts["total"], counts["live"]) == (2, 1)


async def test_two_agents_may_hold_the_same_belief(mem_agent):
    """The index is keyed on `agent_id` too. Memory is per-agent: one agent
    learning a thing must not stop another learning it."""
    other = f"{_AID}-other"
    await mem_agent.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, active) "
        "VALUES ($1, 'Z2', 'tester', '', true)",
        other,
    )
    try:
        await record_memory(mem_agent, _AID, "the fiscal year ends in March")
        await record_memory(mem_agent, other, "the fiscal year ends in March")
        assert (
            await mem_agent.fetchval(
                "SELECT count(*) FROM agent_memory WHERE content = $1",
                "the fiscal year ends in March",
            )
            == 2
        )
    finally:
        await mem_agent.execute("DELETE FROM agent_memory WHERE agent_id = $1", other)
        await mem_agent.execute("DELETE FROM agents WHERE id = $1", other)


async def test_a_memory_too_long_to_index_raw_is_still_written(mem_agent):
    """The index key is `md5(content)`, not `content`.

    `record_memory` caps content at 2000 CHARACTERS, which in a non-Latin
    script is ~6000 bytes — over btree's 2704-byte key limit, whose remedy
    Postgres itself names as "a function index of an MD5 hash of the value".
    Indexing the raw column would turn the owner's own words into an
    exception, and `apply_curiosity_answer` calls `record_memory` bare, so that
    raise would fail the hook and cost the answer's books half.

    The stride below is deliberate, and it is what makes this test falsifiable.
    Postgres compresses an over-long index tuple before giving up on it, so
    `'क' * 2000` (and any short repeating cycle) squeezes under 2704 bytes and
    inserts happily against a RAW-column index — measured. A coprime stride
    over a 20k-codepoint block repeats no 3-gram, does not compress, and lands
    at 6024 bytes.
    """
    long_content = "".join(chr(0x4E00 + (i * 7919) % 20000) for i in range(2000))
    await record_memory(mem_agent, _AID, long_content)
    assert (
        await mem_agent.fetchval("SELECT count(*) FROM agent_memory WHERE agent_id = $1", _AID) == 1
    )


async def test_consolidation_add_of_a_live_belief_is_a_logged_skip(mem_agent):
    """`apply_consolidation` writes `agent_memory` too, and its ops run in ONE
    transaction — "a plan is applied whole or not at all".

    So an ADD colliding with the new index must not RAISE: that would roll back
    every other op in the plan. It takes the path the module already has for a
    statement that touched no row — `applied=False` plus a `skip_reason` on the
    ledger — so an operator reading the ledger sees what happened, and since
    #399 that reason names the index rather than sharing `no_rows_affected`
    with a row that vanished under the plan.
    """
    await record_memory(mem_agent, _AID, "invoices from Zephyrly are groceries")

    out = await apply_consolidation(
        mem_agent,
        _AID,
        [{"op": "ADD", "content": "invoices from Zephyrly are groceries", "apply": True}],
        run_id="r-389",
        dry_run=False,
    )

    assert out == {"applied": 0, "logged": 1, "dry_run": False}
    assert (
        await mem_agent.fetchval("SELECT count(*) FROM agent_memory WHERE agent_id = $1", _AID) == 1
    )
    op = await mem_agent.fetchrow(
        "SELECT applied, skip_reason FROM agent_memory_ops_log WHERE run_id = 'r-389'"
    )
    assert op["applied"] is False
    assert op["skip_reason"] == "duplicate_live_content"
    await mem_agent.execute("DELETE FROM agent_memory_ops_log WHERE agent_id = $1", _AID)


async def test_consolidation_update_onto_a_live_belief_is_a_logged_skip(mem_agent):
    """The THIRD writer. `_SQL_UPDATE` rewrites a live row's content, and
    collapsing two near-duplicate beliefs onto one canonical text is precisely
    what consolidation exists to do — so after migration 028 this is the op
    most likely to hit the index, not the least.

    `apply_consolidation` has no try/except around `_run`, so an unguarded
    raise here aborts the whole one-transaction plan and propagates: the exact
    outcome `_SQL_ADD` is guarded against. Same answer, same skip path.

    Dead while `memory_consolidation_apply_enabled` is unset (every pass is a
    dry run, which writes no agent_memory statement at all) and live the day
    that gate is opened.
    """
    await record_memory(mem_agent, _AID, "the owner banks with HDFC")
    victim = await mem_agent.fetchval(
        "INSERT INTO agent_memory (agent_id, content, importance, source) "
        "VALUES ($1, 'the owner uses an HDFC account', 0.6, 'correction') RETURNING id",
        _AID,
    )

    out = await apply_consolidation(
        mem_agent,
        _AID,
        [
            {
                "op": "UPDATE",
                "id": victim,
                "content": "the owner banks with HDFC",
                "importance": 0.7,
                "apply": True,
            }
        ],
        run_id="r-389-upd",
        dry_run=False,
    )

    assert out == {"applied": 0, "logged": 1, "dry_run": False}
    row = await mem_agent.fetchrow(
        "SELECT content, importance FROM agent_memory WHERE id = $1", victim
    )
    assert row["content"] == "the owner uses an HDFC account"
    assert row["importance"] == pytest.approx(0.6)
    op = await mem_agent.fetchrow(
        "SELECT applied, skip_reason FROM agent_memory_ops_log WHERE run_id = 'r-389-upd'"
    )
    assert op["applied"] is False
    assert op["skip_reason"] == "duplicate_live_content"
    await mem_agent.execute("DELETE FROM agent_memory_ops_log WHERE agent_id = $1", _AID)


async def test_consolidation_update_still_applies_when_nothing_collides(mem_agent):
    """The guard on `_SQL_UPDATE` must block a COLLISION, not every update.

    Three shapes that must all still write, each of which a too-broad guard
    would silently turn into a permanent no-op:

    * a rewrite to text no live row holds — the ordinary merge;
    * a rewrite matching a SUPERSEDED row, since the index is partial and a
      retired belief must not veto the live one;
    * an importance-only edit, where the row's new content equals its OWN
      content — the `id <> $2` exclusion is what keeps the row from finding
      itself and refusing.
    """
    plain = await mem_agent.fetchval(
        "INSERT INTO agent_memory (agent_id, content, importance, source) "
        "VALUES ($1, 'draft belief one', 0.5, 'correction') RETURNING id",
        _AID,
    )
    onto_retired = await mem_agent.fetchval(
        "INSERT INTO agent_memory (agent_id, content, importance, source) "
        "VALUES ($1, 'draft belief two', 0.5, 'correction') RETURNING id",
        _AID,
    )
    unchanged = await mem_agent.fetchval(
        "INSERT INTO agent_memory (agent_id, content, importance, source, superseded_at) "
        "VALUES ($1, 'a belief since withdrawn', 0.5, 'correction', now()) RETURNING id",
        _AID,
    )
    keeps_content = await mem_agent.fetchval(
        "INSERT INTO agent_memory (agent_id, content, importance, source) "
        "VALUES ($1, 'a belief worth more than it says', 0.4, 'correction') RETURNING id",
        _AID,
    )

    out = await apply_consolidation(
        mem_agent,
        _AID,
        [
            {"op": "UPDATE", "id": plain, "content": "one canonical belief", "apply": True},
            {
                "op": "UPDATE",
                "id": onto_retired,
                "content": "a belief since withdrawn",
                "apply": True,
            },
            {
                "op": "UPDATE",
                "id": keeps_content,
                "content": "a belief worth more than it says",
                "importance": 0.9,
                "apply": True,
            },
        ],
        run_id="r-389-upd-ok",
        dry_run=False,
    )

    assert out["applied"] == 3, out
    assert (
        await mem_agent.fetchval("SELECT content FROM agent_memory WHERE id = $1", plain)
        == "one canonical belief"
    )
    assert (
        await mem_agent.fetchval("SELECT content FROM agent_memory WHERE id = $1", onto_retired)
        == "a belief since withdrawn"
    )
    assert await mem_agent.fetchval(
        "SELECT importance FROM agent_memory WHERE id = $1", keeps_content
    ) == pytest.approx(0.9)
    # The retired row is untouched and still retired — it vetoed nothing and
    # was resurrected by nothing.
    assert (
        await mem_agent.fetchval("SELECT superseded_at FROM agent_memory WHERE id = $1", unchanged)
        is not None
    )
    await mem_agent.execute("DELETE FROM agent_memory_ops_log WHERE agent_id = $1", _AID)


# --------------------------------------------------------------------------
# #399 — a plan must mean the same thing whatever order the model emits it in.
# --------------------------------------------------------------------------

_ORDERS = [
    pytest.param(False, id="writer_first"),  # the order the system prompt nudges
    pytest.param(True, id="delete_first"),
]


async def _live(pool) -> list[tuple[int, str]]:
    rows = await pool.fetch(
        "SELECT id, content FROM agent_memory WHERE agent_id = $1 AND superseded_at IS NULL "
        "ORDER BY id",
        _AID,
    )
    return [(int(r["id"]), r["content"]) for r in rows]


@pytest.mark.parametrize("delete_first", _ORDERS)
async def test_consolidation_merge_survives_either_plan_order(mem_agent, delete_first):
    """The canonical merge: rewrite the survivor to the wording that already
    lives on the row being retired, and retire that row.

    Emitted UPDATE-first, the UPDATE collided with the still-live donor, was
    skipped as a duplicate, and the DELETE then retired the donor anyway — so
    the merged wording left the live set entirely while `applied` still said 1.
    Same plan, model-chosen order, different memory.
    """
    survivor = await mem_agent.fetchval(
        "INSERT INTO agent_memory (agent_id, content, importance, source) "
        "VALUES ($1, 'the owner banks with HDFC', 0.6, 'correction') RETURNING id",
        _AID,
    )
    donor = await mem_agent.fetchval(
        "INSERT INTO agent_memory (agent_id, content, importance, source) "
        "VALUES ($1, 'the owner banks with HDFC (salary account)', 0.5, 'correction') "
        "RETURNING id",
        _AID,
    )
    update = {
        "op": "UPDATE",
        "id": survivor,
        "content": "the owner banks with HDFC (salary account)",
        "importance": 0.7,
        "apply": True,
    }
    retire = {"op": "DELETE", "id": donor, "merged_into": survivor, "apply": True}
    plan = [retire, update] if delete_first else [update, retire]

    out = await apply_consolidation(mem_agent, _AID, plan, run_id="r-399-upd", dry_run=False)

    assert out["applied"] == 2, out
    assert await _live(mem_agent) == [(survivor, "the owner banks with HDFC (salary account)")]
    assert (
        await mem_agent.fetchval("SELECT superseded_by FROM agent_memory WHERE id = $1", donor)
        == survivor
    )
    log = {
        r["op"]: (r["applied"], r["skip_reason"])
        for r in await mem_agent.fetch(
            "SELECT op, applied, skip_reason FROM agent_memory_ops_log WHERE run_id = 'r-399-upd'"
        )
    }
    assert log == {"UPDATE": (True, None), "DELETE": (True, None)}


@pytest.mark.parametrize("delete_first", _ORDERS)
async def test_consolidation_add_survives_either_plan_order(mem_agent, delete_first):
    """The ADD shape of the same bug, and the worse one: emitted ADD-first the
    insert hit the live-content index, was dropped, and the DELETE then retired
    the only row that held the text — leaving ZERO live rows carrying it while
    `applied` reported 1."""
    donor = await mem_agent.fetchval(
        "INSERT INTO agent_memory (agent_id, content, importance, source) "
        "VALUES ($1, 'renewals are due in March', 0.5, 'correction') RETURNING id",
        _AID,
    )
    add = {"op": "ADD", "content": "renewals are due in March", "importance": 0.7, "apply": True}
    retire = {"op": "DELETE", "id": donor, "apply": True}
    plan = [retire, add] if delete_first else [add, retire]

    out = await apply_consolidation(mem_agent, _AID, plan, run_id="r-399-add", dry_run=False)

    assert out["applied"] == 2, out
    live = await _live(mem_agent)
    assert [c for _, c in live] == ["renewals are due in March"]
    assert live[0][0] != donor, "the retired row must not be the one still live"


async def test_consolidation_duplicate_skip_has_its_own_reason(mem_agent):
    """A write skipped because a live row already holds the text is a different
    event from a row that vanished under the plan, and the dry-run ledger is
    the evidence an operator reads before opening the apply gate. Both used to
    log `no_rows_affected`."""
    await record_memory(mem_agent, _AID, "the owner drives a diesel")
    rival = await mem_agent.fetchval(
        "INSERT INTO agent_memory (agent_id, content, importance, source) "
        "VALUES ($1, 'the owner drives a car', 0.5, 'correction') RETURNING id",
        _AID,
    )

    out = await apply_consolidation(
        mem_agent,
        _AID,
        [
            {"op": "ADD", "content": "the owner drives a diesel", "apply": True},
            {"op": "UPDATE", "id": rival, "content": "the owner drives a diesel", "apply": True},
        ],
        run_id="r-399-dup",
        dry_run=False,
    )

    assert out["applied"] == 0, out
    assert {
        r["op"]: r["skip_reason"]
        for r in await mem_agent.fetch(
            "SELECT op, skip_reason FROM agent_memory_ops_log WHERE run_id = 'r-399-dup'"
        )
    } == {"ADD": "duplicate_live_content", "UPDATE": "duplicate_live_content"}


async def test_consolidation_row_retired_by_its_own_plan_is_not_a_duplicate(mem_agent):
    """The OTHER cause of a zero-row write keeps the old reason.

    A plan that retires a row and also rewrites it is self-contradictory; the
    retire wins (it runs first) and the rewrite touches nothing. Nothing
    collided, so calling that a duplicate would send an operator hunting for a
    rival belief that does not exist.

    `rival` makes BOTH causes true at once — the row is gone AND a live row
    holds the text the rewrite wanted. The UPDATE's own WHERE clause fails on
    the row first and never reaches its duplicate guard, so that is the reason
    reported; asking the two questions in the order the statement asks them is
    what keeps the ledger honest here.
    """
    doomed = await mem_agent.fetchval(
        "INSERT INTO agent_memory (agent_id, content, importance, source) "
        "VALUES ($1, 'a belief in two minds', 0.5, 'correction') RETURNING id",
        _AID,
    )
    rival = await mem_agent.fetchval(
        "INSERT INTO agent_memory (agent_id, content, importance, source) "
        "VALUES ($1, 'a belief rewritten', 0.5, 'correction') RETURNING id",
        _AID,
    )

    out = await apply_consolidation(
        mem_agent,
        _AID,
        [
            {"op": "UPDATE", "id": doomed, "content": "a belief rewritten", "apply": True},
            {"op": "DELETE", "id": doomed, "apply": True},
        ],
        run_id="r-399-gone",
        dry_run=False,
    )

    assert out["applied"] == 1, out
    assert await _live(mem_agent) == [(rival, "a belief rewritten")]
    assert {
        r["op"]: r["skip_reason"]
        for r in await mem_agent.fetch(
            "SELECT op, skip_reason FROM agent_memory_ops_log WHERE run_id = 'r-399-gone'"
        )
    } == {"UPDATE": "no_rows_affected", "DELETE": None}


async def test_dry_run_ledger_shows_which_writes_the_index_would_refuse(mem_agent):
    """Dry run is the long-running mode and its ledger is the flip decision's
    evidence. It writes no `agent_memory` statement, so before this it could
    not show the guard firing at all — every approved op logged a bare
    `applied=false` whether it would have written or been refused.

    The prediction must also account for the plan's own retirements, or it
    reports a skip that apply mode would not perform.
    """
    await record_memory(mem_agent, _AID, "a belief with a rival")
    donor = await mem_agent.fetchval(
        "INSERT INTO agent_memory (agent_id, content, importance, source) "
        "VALUES ($1, 'a belief this plan retires', 0.5, 'correction') RETURNING id",
        _AID,
    )

    out = await apply_consolidation(
        mem_agent,
        _AID,
        [
            {"op": "ADD", "content": "a belief with a rival", "apply": True},
            {"op": "ADD", "content": "a belief this plan retires", "apply": True},
            {"op": "ADD", "content": "a belief nobody holds", "apply": True},
            {"op": "DELETE", "id": donor, "apply": True},
        ],
        run_id="r-399-dry",
        dry_run=True,
    )

    assert out == {"applied": 0, "logged": 4, "dry_run": True}
    assert len(await _live(mem_agent)) == 2, "a dry run writes nothing"
    assert {
        r["after_content"]: r["skip_reason"]
        for r in await mem_agent.fetch(
            "SELECT after_content, skip_reason FROM agent_memory_ops_log "
            "WHERE run_id = 'r-399-dry' AND op = 'ADD'"
        )
    } == {
        "a belief with a rival": "duplicate_live_content",
        "a belief this plan retires": None,
        "a belief nobody holds": None,
    }
