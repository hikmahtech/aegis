"""A4 — the safety rails that make an APPLIED consolidation recoverable.

A3 could not write at all, so its tests only had to prove "nothing happened".
A4 can write, so these tests have to prove the harder thing: that a *wrong*
plan is bounded and reversible.

The rig itself is under test first. `test_destructive_assertions_detect_a_real_write`
runs the two assertions the whole safety suite leans on
(`spy.mutations == []` and "snapshot unchanged") against a pass that IS
allowed to write, and asserts both would now fail. A green safety suite that
cannot see a write is worse than no suite.

Every guard below is exercised in ISOLATION — aged rows for the importance and
dedupe-marker tests, plain rows for the recency test, a quota-clearing row
count for the protection tests — so that no two rails can mask each other and
report a false pass.
"""

from __future__ import annotations

import itertools
import json

import pytest
import pytest_asyncio
from aegis_worker.activities.memory import (
    _MAX_APPLY_OPS,
    _MIN_MEMORIES_TO_APPLY,
    MemoryActivities,
    decide_ops,
)
from temporalio.testing import ActivityEnvironment

from tests.worker.activities.test_memory_consolidation import _SpyPool, _StubLLM

_AID = "zza4-agent"
_OTHER = "zza4-other"


async def _snapshot(pool, agent_id: str) -> list[tuple]:
    rows = await pool.fetch(
        "SELECT id, agent_id, content, importance, source, created_at, "
        "superseded_at, superseded_by, last_consolidated_at "
        "FROM agent_memory WHERE agent_id = $1 ORDER BY id",
        agent_id,
    )
    return [tuple(r) for r in rows]


# Row content is unique across CALLS, not just within one.
#
# `test_recent_rows_are_never_touched` seeds the same agent twice, and a
# per-call `range(n)` made its "fresh" row byte-identical to the first batch's
# row 0. That cost the old assertions nothing — `decide_ops` and
# `_protection_reason` key on id, created_at, importance and the gmail marker,
# and nothing in either compares content across rows — so those tests were
# sound as written.
#
# It is migration 028 that makes it matter: a partial unique index on live
# `(agent_id, md5(content))` turns the second call's INSERT into a
# UniqueViolationError. The test would ERROR in the fixture rather than pass
# falsely, so this is a required consequence of the migration, not a repair of
# a test that was proving nothing.
_seed_seq = itertools.count()


async def _seed_rows(pool, agent_id: str, n: int, *, age_days: int = 10) -> list[int]:
    """`n` plain rows, aged past every recency guard unless told otherwise."""
    ids = []
    for i in itertools.islice(_seed_seq, n):
        ids.append(
            await pool.fetchval(
                "INSERT INTO agent_memory (agent_id, content, importance, source, created_at) "
                "VALUES ($1, $2, 0.5, 'correction', now() - make_interval(days => $3)) "
                "RETURNING id",
                agent_id,
                f"lesson number {i} for {agent_id}",
                age_days,
            )
        )
    return ids


async def _wipe(pool):
    for aid in (_AID, _OTHER):
        await pool.execute("DELETE FROM agent_memory_ops_log WHERE agent_id = $1", aid)
        await pool.execute("DELETE FROM agent_memory WHERE agent_id = $1", aid)
        await pool.execute("DELETE FROM llm_calls WHERE agent_id = $1", aid)
        await pool.execute("DELETE FROM agents WHERE id = $1", aid)


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await _wipe(db_pool)
    for aid in (_AID, _OTHER):
        await db_pool.execute(
            "INSERT INTO agents (id, name, role, system_prompt_path, active) "
            "VALUES ($1, 'Z', 'tester', '', true)",
            aid,
        )
    yield db_pool
    await _wipe(db_pool)


def _delete_plan(ids: list[int]) -> str:
    return json.dumps([{"op": "DELETE", "id": i, "reason": "redundant"} for i in ids])


def _applying(pool_obj, plan: str) -> MemoryActivities:
    """An activity with BOTH gates open — used only where a write is the point."""
    return MemoryActivities(db_pool=pool_obj, llm_client=_StubLLM(plan), apply_enabled=True)


async def _run_apply(act, *, max_ops_pct: float = 0.25, min_age_hours: int = 0) -> dict:
    return await ActivityEnvironment().run(
        act.consolidate_agent_memories, _AID, False, max_ops_pct, min_age_hours
    )


async def _ops_log(pool_obj, agent_id: str = _AID) -> list[dict]:
    rows = await pool_obj.fetch(
        "SELECT op, memory_id, before_content, after_content, dry_run, applied, skip_reason "
        "FROM agent_memory_ops_log WHERE agent_id = $1 ORDER BY id",
        agent_id,
    )
    return [dict(r) for r in rows]


# ==========================================================================
# 0. THE RIG. If these fail, nothing below means anything.
# ==========================================================================


async def test_destructive_assertions_detect_a_real_write(pool):
    """Issue #201 point 4, proven rather than asserted.

    Take the two assertions every destructive test in this suite relies on and
    run them against a pass that is actually allowed to write. Both must now
    be FALSE. If this test starts failing, the safety net has gone blind and
    the green ticks elsewhere are meaningless.

    This also exercises the `acquire()` recording proxy: A4 applies inside a
    transaction, so the mutating SQL never touches `pool.execute` — it is only
    visible if the spy wraps the connection too.
    """
    ids = await _seed_rows(pool, _AID, 20)
    before = await _snapshot(pool, _AID)
    spy = _SpyPool(pool)

    out = await _run_apply(_applying(spy, _delete_plan(ids[:3])))

    assert out["applied"] == 3, out
    # The two load-bearing assertions, inverted:
    assert spy.mutations != [], "the SQL spy did not see a real write"
    assert await _snapshot(pool, _AID) != before, "the snapshot did not see a real write"
    # ...and the write it saw came through the transaction connection, not the pool.
    assert any("superseded_at = now()" in q for q in spy.mutations)


async def test_apply_mode_writes_only_through_the_soft_retire_statement(pool):
    """The only mutating verbs a plan can reach are the three vetted ones —
    no raw DELETE FROM agent_memory anywhere on the path."""
    ids = await _seed_rows(pool, _AID, 20)
    spy = _SpyPool(pool)

    await _run_apply(_applying(spy, _delete_plan(ids[:2])))

    for q in spy.mutations:
        assert q.lstrip().upper().startswith(("UPDATE AGENT_MEMORY", "INSERT INTO AGENT_MEMORY"))
    assert not any("DELETE FROM agent_memory" in q for q in spy.queries)


# ==========================================================================
# 1. QUOTAS — a plan that is too big changes NOTHING (not "the first N").
# ==========================================================================


async def test_plan_exceeding_quota_changes_nothing(pool):
    """Spec acceptance: a 40-row agent, an LLM proposing 20 deletes ⇒ the batch
    is rejected wholesale, zero rows change, and every proposed op still lands
    in the ledger."""
    ids = await _seed_rows(pool, _AID, 40)
    before = await _snapshot(pool, _AID)
    spy = _SpyPool(pool)

    out = await _run_apply(_applying(spy, _delete_plan(ids[:20])))

    assert out["status"] == "quota_exceeded_pct"
    assert out["refusal"] == "quota_exceeded_pct"
    assert out["applied"] == 0
    assert spy.mutations == [], f"refused plan still issued SQL: {spy.mutations}"
    assert await _snapshot(pool, _AID) == before

    log = await _ops_log(pool)
    assert len(log) == 20, "one ledger row per PROPOSED op, refused or not"
    assert {r["skip_reason"] for r in log} == {"quota_exceeded_pct"}
    assert not any(r["applied"] for r in log)
    assert {r["dry_run"] for r in log} == {False}, "dry_run column reflects the requested mode"


async def test_quota_is_a_cliff_not_a_truncation(pool):
    """The failure mode this rail exists to prevent: applying the first N ops
    of an over-budget plan, i.e. a half-done consolidation.

    40 rows at 25% ⇒ a budget of 10. Ten deletes apply; ELEVEN apply ZERO —
    not ten. Both halves are asserted so the test cannot pass by the plan
    never applying at all.
    """
    ids = await _seed_rows(pool, _AID, 40)

    at_budget = await _run_apply(_applying(pool, _delete_plan(ids[:10])))
    assert at_budget["status"] == "applied"
    assert at_budget["applied"] == 10

    # Re-seed: the first 10 are now retired and out of the live set.
    await _wipe(pool)
    for aid in (_AID, _OTHER):
        await pool.execute(
            "INSERT INTO agents (id, name, role, system_prompt_path, active) "
            "VALUES ($1, 'Z', 'tester', '', true)",
            aid,
        )
    ids = await _seed_rows(pool, _AID, 40)
    before = await _snapshot(pool, _AID)

    over_budget = await _run_apply(_applying(pool, _delete_plan(ids[:11])))
    assert over_budget["status"] == "quota_exceeded_pct"
    assert over_budget["applied"] == 0, "over-budget plan was truncated instead of refused"
    assert await _snapshot(pool, _AID) == before


async def test_absolute_op_cap_fires_independently_of_the_percentage(pool):
    """110 rows at 25% is a budget of 27, so the percentage rail is NOT what
    stops a 26-op plan — the absolute cap is. Proving them separately keeps
    the two guards from masking each other."""
    ids = await _seed_rows(pool, _AID, 110)
    n = _MAX_APPLY_OPS + 1
    assert n <= int(110 * 0.25), "test would not isolate the absolute cap"
    before = await _snapshot(pool, _AID)

    out = await _run_apply(_applying(pool, _delete_plan(ids[:n])))

    assert out["status"] == "quota_exceeded_abs"
    assert out["applied"] == 0
    assert await _snapshot(pool, _AID) == before


async def test_too_few_memories_refuses_everything(pool):
    """Below the floor, one op is a large fraction of everything the agent
    knows and the percentage bound stops meaning anything."""
    ids = await _seed_rows(pool, _AID, _MIN_MEMORIES_TO_APPLY - 1)
    before = await _snapshot(pool, _AID)

    out = await _run_apply(_applying(pool, _delete_plan(ids[:1])))

    assert out["status"] == "too_few_memories"
    assert out["applied"] == 0
    assert await _snapshot(pool, _AID) == before


# ==========================================================================
# 2. PROTECTED ROWS — each guard proven on its own.
# ==========================================================================


async def test_recent_rows_are_never_touched(pool):
    """A row created inside `min_age_hours` is skipped. Everything else about
    this row is unremarkable (importance 0.5, no marker), so recency is
    demonstrably the only rail that can be firing."""
    old_ids = await _seed_rows(pool, _AID, 15)
    fresh = (await _seed_rows(pool, _AID, 1, age_days=0))[0]

    out = await _run_apply(
        _applying(pool, _delete_plan([fresh, old_ids[0]])), min_age_hours=24
    )

    assert out["applied"] == 1, "the aged row should still have applied"
    log = {r["memory_id"]: r for r in await _ops_log(pool)}
    assert log[fresh]["skip_reason"] == "protected_recent"
    assert log[fresh]["applied"] is False
    assert log[old_ids[0]]["applied"] is True
    row = await pool.fetchrow("SELECT superseded_at FROM agent_memory WHERE id = $1", fresh)
    assert row["superseded_at"] is None


async def test_high_importance_rows_are_protected(pool):
    """importance >= 0.9. The row is aged 10 days, so the recency guard cannot
    be the one firing."""
    ids = await _seed_rows(pool, _AID, 15)
    precious = ids[0]
    await pool.execute("UPDATE agent_memory SET importance = 0.95 WHERE id = $1", precious)

    out = await _run_apply(
        _applying(pool, _delete_plan([precious, ids[1]])), min_age_hours=24
    )

    assert out["applied"] == 1
    log = {r["memory_id"]: r for r in await _ops_log(pool)}
    assert log[precious]["skip_reason"] == "protected_importance"
    row = await pool.fetchrow("SELECT superseded_at FROM agent_memory WHERE id = $1", precious)
    assert row["superseded_at"] is None


async def test_gmail_dedupe_marker_rows_are_protected_and_stay_idempotent(pool):
    """Spec acceptance: a merge op targeting a row carrying `[gmail:<id>]` is
    skipped, and `record_gmail_triage_correction` remains idempotent for that
    email id afterwards.

    The row is aged and importance 0.5, so neither the recency nor the
    importance rail can be responsible for the skip.
    """
    from aegis.services.memory import record_gmail_triage_correction

    ids = await _seed_rows(pool, _AID, 15)
    assert await record_gmail_triage_correction(pool, _AID, "msg-77", "Invoice", "spam", "inbox")
    gmail_id = await pool.fetchval(
        "SELECT id FROM agent_memory WHERE agent_id = $1 AND content LIKE '%[gmail:msg-77]'", _AID
    )
    await pool.execute(
        "UPDATE agent_memory SET created_at = now() - interval '10 days', importance = 0.5 "
        "WHERE id = $1",
        gmail_id,
    )

    plan = json.dumps(
        [
            {"op": "UPDATE", "id": gmail_id, "content": "merged triage note", "importance": 0.6},
            {"op": "DELETE", "id": ids[0], "reason": "dup"},
        ]
    )
    out = await _run_apply(_applying(pool, plan), min_age_hours=24)

    assert out["applied"] == 1  # only the ordinary row
    log = {r["memory_id"]: r for r in await _ops_log(pool)}
    assert log[gmail_id]["skip_reason"] == "protected_dedupe_marker"

    row = await pool.fetchrow(
        "SELECT content, superseded_at FROM agent_memory WHERE id = $1", gmail_id
    )
    assert "[gmail:msg-77]" in row["content"], "the dedupe marker was rewritten"
    assert row["superseded_at"] is None
    # Still idempotent: a re-run writes no second row.
    assert (
        await record_gmail_triage_correction(pool, _AID, "msg-77", "Invoice", "spam", "inbox")
        is False
    )


async def test_protection_survives_even_when_the_row_is_retired(pool):
    """Belt and braces on idempotence: even if a gmail row somehow got
    retired, the dedupe check must still see it (it is deliberately not
    filtered on superseded_at) rather than resurrect a duplicate."""
    from aegis.services.memory import record_gmail_triage_correction

    await _seed_rows(pool, _AID, 12)
    assert await record_gmail_triage_correction(pool, _AID, "msg-88", "Bill", "spam", "inbox")
    await pool.execute(
        "UPDATE agent_memory SET superseded_at = now() WHERE agent_id = $1 "
        "AND content LIKE '%[gmail:msg-88]'",
        _AID,
    )

    assert (
        await record_gmail_triage_correction(pool, _AID, "msg-88", "Bill", "spam", "inbox")
        is False
    )


# ==========================================================================
# 3. MALFORMED / HOSTILE LLM OUTPUT — under APPLY mode this time.
# ==========================================================================


@pytest.mark.parametrize(
    "label,response",
    [
        ("truncated", json.dumps([{"op": "DELETE", "id": 1, "reason": "dup"}])[:-3]),
        ("prose", "Sure! I would delete everything."),
        ("empty", ""),
        ("null", "null"),
        ("object_not_list", json.dumps({"op": "DELETE", "id": 1})),
        ("nested_junk", json.dumps([[{"op": "DELETE", "id": 1}]])),
        ("sql_injection", json.dumps([{"op": "DELETE; TRUNCATE agent_memory; --", "id": 1}])),
    ],
)
async def test_malformed_output_applies_nothing(pool, label, response):
    """A3 proved this with no apply path behind it. Re-proven with both gates
    OPEN, which is the only version that means anything."""
    await _seed_rows(pool, _AID, 20)
    before = await _snapshot(pool, _AID)
    spy = _SpyPool(pool)

    out = await _run_apply(_applying(spy, response))

    assert out["applied"] == 0, f"[{label}] applied something"
    assert out["ops"] == [], f"[{label}] produced ops"
    assert spy.mutations == [], f"[{label}] issued mutating SQL: {spy.mutations}"
    assert await _snapshot(pool, _AID) == before, f"[{label}] rows changed"


async def test_oversized_plan_is_rejected_whole_not_truncated(pool):
    """A 500-op response must not become "the first 200 ops". `_validate_ops`
    drops the lot; nothing downstream sees a plan at all."""
    ids = await _seed_rows(pool, _AID, 40)
    huge = json.dumps([{"op": "DELETE", "id": ids[i % 40], "reason": "x"} for i in range(500)])
    before = await _snapshot(pool, _AID)

    out = await _run_apply(_applying(pool, huge))

    assert out["ops"] == []
    assert out["applied"] == 0
    assert await _snapshot(pool, _AID) == before
    assert await _ops_log(pool) == [], "a rejected payload should not fill the ledger"


async def test_cross_agent_ids_are_rejected_by_validation(pool):
    """Guard 1 of 2 on ownership: `_validate_ops` never emits an op naming a
    row the agent does not own."""
    await _seed_rows(pool, _AID, 15)
    foreign = (await _seed_rows(pool, _OTHER, 1))[0]
    before_foreign = await _snapshot(pool, _OTHER)

    out = await _run_apply(_applying(pool, _delete_plan([foreign])))

    assert out["ops"] == []
    assert out["skipped"] == 1
    assert await _snapshot(pool, _OTHER) == before_foreign


async def test_cross_agent_ids_are_rejected_again_at_the_sql_layer(pool):
    """Guard 2 of 2, proven INDEPENDENTLY by bypassing guard 1 entirely:
    hand `apply_consolidation` a fully-approved decision naming another
    agent's row. Every statement is scoped by agent_id in SQL, so the foreign
    row is untouched even though the caller said `apply: True`."""
    from aegis.services.memory import apply_consolidation

    await _seed_rows(pool, _AID, 15)
    foreign = (await _seed_rows(pool, _OTHER, 1))[0]
    before_foreign = await _snapshot(pool, _OTHER)

    out = await apply_consolidation(
        pool,
        _AID,
        [
            {
                "op": "DELETE",
                "id": foreign,
                "reason": "hostile",
                "merged_into": None,
                "apply": True,
                "skip_reason": None,
            }
        ],
        dry_run=False,
    )

    assert out["applied"] == 0
    assert await _snapshot(pool, _OTHER) == before_foreign
    log = await _ops_log(pool)
    assert [r["skip_reason"] for r in log] == ["row_not_found"]


# ==========================================================================
# 4. SOFT RETIRE — invisible to readers, still recoverable.
# ==========================================================================


async def test_retired_row_is_hidden_from_reads_but_restorable(pool):
    from aegis.services.memory import all_memories, recent_memories

    ids = await _seed_rows(pool, _AID, 20)
    target = ids[0]
    target_text = await pool.fetchval(
        "SELECT content FROM agent_memory WHERE id = $1", target
    )

    await _run_apply(_applying(pool, _delete_plan([target])))

    assert target_text not in await recent_memories(pool, _AID, limit=100)
    assert target not in {m["id"] for m in await all_memories(pool, _AID)}
    # Still there, and marked.
    row = await pool.fetchrow(
        "SELECT content, superseded_at, last_consolidated_at FROM agent_memory WHERE id = $1",
        target,
    )
    assert row is not None, "soft retire deleted the row"
    assert row["content"] == target_text, "content was destroyed, not retired"
    assert row["superseded_at"] is not None
    assert row["last_consolidated_at"] is not None

    # One statement restores it.
    await pool.execute(
        "UPDATE agent_memory SET superseded_at = NULL, superseded_by = NULL WHERE id = $1", target
    )
    assert target_text in await recent_memories(pool, _AID, limit=100)


async def test_merged_into_records_the_successor(pool):
    """Provenance: when the plan says which row absorbed this one, that link
    is stored so the merge can be read back later."""
    ids = await _seed_rows(pool, _AID, 20)
    plan = json.dumps(
        [{"op": "DELETE", "id": ids[0], "reason": "dup", "merged_into": ids[1]}]
    )

    await _run_apply(_applying(pool, plan))

    assert (
        await pool.fetchval("SELECT superseded_by FROM agent_memory WHERE id = $1", ids[0])
        == ids[1]
    )


async def test_prune_does_not_hard_delete_freshly_retired_rows(pool):
    """The trap this rail exists for: MemoryReflectionFlow runs consolidation
    and then the `keep` cap in the SAME pass. A retire-blind prune would
    hard-delete everything consolidation just retired, minutes later, and
    "soft retire" would be a lie within one nightly run.

    The retired rows are deliberately the LOWEST-ranked ones (importance 0.1
    against 0.8), so a retire-blind prune at keep=15 would evict exactly them.
    Without that the guard is invisible: with 20 rows and a keep of 50 both
    implementations delete nothing and the test passes either way.
    """
    ids = await _seed_rows(pool, _AID, 20)
    await pool.execute(
        "UPDATE agent_memory SET importance = 0.8 WHERE agent_id = $1", _AID
    )
    await pool.execute(
        "UPDATE agent_memory SET importance = 0.1 WHERE id = ANY($1::bigint[])", ids[:5]
    )
    await _run_apply(_applying(pool, _delete_plan(ids[:5])), max_ops_pct=0.3)
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM agent_memory WHERE agent_id = $1 AND superseded_at IS NOT NULL",
            _AID,
        )
        == 5
    )

    act = MemoryActivities(db_pool=pool)
    await ActivityEnvironment().run(act.prune_agent_memories, 15, 0)

    survivors = await pool.fetch(
        "SELECT id FROM agent_memory WHERE agent_id = $1 AND superseded_at IS NOT NULL", _AID
    )
    assert {r["id"] for r in survivors} == set(ids[:5]), (
        "the nightly cap hard-deleted rows consolidation had only retired"
    )
    assert await pool.fetchval(
        "SELECT count(*) FROM agent_memory WHERE agent_id = $1", _AID
    ) == 20


async def test_retired_rows_do_not_consume_the_keep_budget(pool):
    """A retired row is invisible to every reader, so holding a slot in the
    cap would silently shrink working memory.

    The retired rows are the HIGHEST-ranked ones here (0.8 against 0.4), so a
    retire-blind prune would spend 5 of its 10 slots on invisible rows and
    leave only 5 live memories instead of 10.
    """
    ids = await _seed_rows(pool, _AID, 20)
    await pool.execute("UPDATE agent_memory SET importance = 0.4 WHERE agent_id = $1", _AID)
    await pool.execute(
        "UPDATE agent_memory SET importance = 0.8 WHERE id = ANY($1::bigint[])", ids[:5]
    )
    await _run_apply(_applying(pool, _delete_plan(ids[:5])), max_ops_pct=0.3)

    act = MemoryActivities(db_pool=pool)
    await ActivityEnvironment().run(act.prune_agent_memories, 10, 0)

    live = await pool.fetchval(
        "SELECT count(*) FROM agent_memory WHERE agent_id = $1 AND superseded_at IS NULL", _AID
    )
    assert live == 10, "retired rows were counted against the keep budget"


async def test_hard_purge_is_disabled_by_default(pool):
    """`retire_grace_days = 0` is the shipped default and must be a no-op:
    deploying A4 must not start destroying anything."""
    from aegis.services.memory import purge_retired_memories

    ids = await _seed_rows(pool, _AID, 20)
    await _run_apply(_applying(pool, _delete_plan(ids[:3])))
    await pool.execute(
        "UPDATE agent_memory SET superseded_at = now() - interval '400 days' "
        "WHERE agent_id = $1 AND superseded_at IS NOT NULL",
        _AID,
    )

    assert await purge_retired_memories(pool, _AID, 0) == 0
    assert await purge_retired_memories(pool, _AID, -1) == 0
    act = MemoryActivities(db_pool=pool)
    await ActivityEnvironment().run(act.prune_agent_memories, 50, 0)
    assert (
        await pool.fetchval("SELECT count(*) FROM agent_memory WHERE agent_id = $1", _AID) == 20
    )


async def test_hard_purge_respects_the_grace_window_and_spares_live_rows(pool):
    from aegis.services.memory import purge_retired_memories

    ids = await _seed_rows(pool, _AID, 20)
    await _run_apply(_applying(pool, _delete_plan(ids[:4])))
    # Two retired a year ago, two retired just now.
    await pool.execute(
        "UPDATE agent_memory SET superseded_at = now() - interval '400 days' "
        "WHERE id = ANY($1::bigint[])",
        ids[:2],
    )

    assert await purge_retired_memories(pool, _AID, 30) == 2
    assert (
        await pool.fetchval("SELECT count(*) FROM agent_memory WHERE agent_id = $1", _AID) == 18
    ), "the purge took rows outside the retired-and-aged set"
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM agent_memory WHERE agent_id = $1 AND superseded_at IS NULL",
            _AID,
        )
        == 16
    ), "a live row was hard-deleted"
    # The ledger still holds the text of what was destroyed.
    log = {r["memory_id"]: r for r in await _ops_log(pool)}
    assert log[ids[0]]["before_content"], "no reconstruction path for a purged row"


# ==========================================================================
# 5. THE KILL SWITCH.
# ==========================================================================


async def test_env_switch_off_means_zero_mutations(pool):
    """Gate 2 closed with gate 1 open — the state an operator reaches by
    unsetting the env var without touching /admin/flows."""
    ids = await _seed_rows(pool, _AID, 20)
    before = await _snapshot(pool, _AID)
    spy = _SpyPool(pool)
    act = MemoryActivities(db_pool=spy, llm_client=_StubLLM(_delete_plan(ids[:2])), apply_enabled=False)

    out = await ActivityEnvironment().run(act.consolidate_agent_memories, _AID, False, 0.25, 0)

    assert out["status"] == "apply_disabled"
    assert out["dry_run"] is True
    assert out["applied"] == 0
    assert spy.mutations == []
    assert await _snapshot(pool, _AID) == before


async def test_config_gate_off_means_zero_mutations(pool):
    """Gate 1 closed with gate 2 open — the env var is set but the flow config
    still says dry_run. Proving each gate alone is sufficient is what stops
    the two from masking each other."""
    ids = await _seed_rows(pool, _AID, 20)
    before = await _snapshot(pool, _AID)
    spy = _SpyPool(pool)
    act = MemoryActivities(db_pool=spy, llm_client=_StubLLM(_delete_plan(ids[:2])), apply_enabled=True)

    out = await ActivityEnvironment().run(act.consolidate_agent_memories, _AID, True, 0.25, 0)

    assert out["dry_run"] is True
    assert out["applied"] == 0
    assert spy.mutations == []
    assert await _snapshot(pool, _AID) == before
    # ...and the same activity DOES write once gate 1 opens, so the assertion
    # above is not passing for some unrelated reason.
    assert (await _run_apply(_applying(pool, _delete_plan(ids[:2]))))["applied"] == 2


async def test_apply_enabled_defaults_to_false():
    """Deploying A4 must not enable anything by itself."""
    from aegis.config import Settings

    assert MemoryActivities(db_pool=None).apply_enabled is False
    assert Settings.model_fields["memory_consolidation_apply_enabled"].default is False


# ==========================================================================
# 6. AUDIT TRAIL.
# ==========================================================================


async def test_ledger_reconstructs_the_prior_state(pool):
    """"What changed, from what, to what, which run, when" — enough to undo a
    bad night by hand."""
    ids = await _seed_rows(pool, _AID, 20)
    original = await pool.fetchval("SELECT content FROM agent_memory WHERE id = $1", ids[0])
    plan = json.dumps(
        [
            {"op": "UPDATE", "id": ids[0], "content": "merged text", "importance": 0.8},
            {"op": "DELETE", "id": ids[1], "reason": "dup"},
            {"op": "ADD", "content": "a generalisation", "importance": 0.6},
        ]
    )

    await _run_apply(_applying(pool, plan))

    log = {(r["op"], r["memory_id"]): r for r in await _ops_log(pool)}
    upd = log[("UPDATE", ids[0])]
    assert upd["before_content"] == original
    assert upd["after_content"] == "merged text"
    assert upd["applied"] is True
    assert log[("DELETE", ids[1])]["before_content"]
    assert log[("ADD", None)]["after_content"] == "a generalisation"

    row = await pool.fetchrow(
        "SELECT run_id, created_at, before_importance, after_importance "
        "FROM agent_memory_ops_log WHERE agent_id = $1 AND op = 'UPDATE'",
        _AID,
    )
    assert row["created_at"] is not None
    assert row["before_importance"] == pytest.approx(0.5)
    assert row["after_importance"] == pytest.approx(0.8)


async def test_dry_run_logs_every_op_without_writing(pool):
    """Dry-run is the long-running default and its whole purpose is producing
    this evidence."""
    ids = await _seed_rows(pool, _AID, 20)
    before = await _snapshot(pool, _AID)
    act = MemoryActivities(db_pool=pool, llm_client=_StubLLM(_delete_plan(ids[:3])))

    await ActivityEnvironment().run(act.consolidate_agent_memories, _AID, True, 0.25, 0)

    log = await _ops_log(pool)
    assert len(log) == 3
    assert all(r["dry_run"] and not r["applied"] for r in log)
    assert all(r["before_content"] for r in log), "dry-run rows must carry the pre-image"
    assert await _snapshot(pool, _AID) == before


async def test_ops_log_is_deliberately_not_pruned():
    """Tripwire. `prune_old_records` has no predicate support, so a retention
    entry here would delete `applied = true` rows — the only record of what an
    LLM changed, and the last copy of any row later hard-purged. If this table
    ever needs pruning, teach the janitor a predicate first."""
    from aegis_worker.activities.cleanup import _ALLOWED_TABLES
    from aegis_worker.flows.cleanup import _DEFAULT_RETENTIONS

    assert "agent_memory_ops_log" not in _DEFAULT_RETENTIONS
    assert "agent_memory_ops_log" not in _ALLOWED_TABLES


# ==========================================================================
# 7. decide_ops in isolation — the policy, with no DB in the way.
# ==========================================================================


def _mem(mid: int, *, importance: float = 0.5, content: str = "x", age_days: int = 10) -> dict:
    from datetime import UTC, datetime, timedelta

    return {
        "id": mid,
        "content": content,
        "importance": importance,
        "source": "correction",
        "created_at": datetime.now(UTC) - timedelta(days=age_days),
    }


def test_decide_ops_never_drops_an_op_from_the_ledger():
    """A refused or protected op still has to reach `agent_memory_ops_log` —
    silence about a refusal is how a bad plan goes unnoticed."""
    memories = [_mem(i) for i in range(1, 41)]
    ops = [{"op": "DELETE", "id": i, "reason": "x"} for i in range(1, 21)]

    decisions, refusal = decide_ops(memories, ops, max_ops_pct=0.25, min_age_hours=24)

    assert refusal == "quota_exceeded_pct"
    assert len(decisions) == len(ops)
    assert all(d["apply"] is False for d in decisions)


def test_decide_ops_counts_the_proposal_not_the_survivors():
    """A plan that wants to rewrite half the agent's memory is a bad plan even
    if most of its targets happen to be protected. Counting after protection
    would let an aggressive plan through whenever it aimed badly enough."""
    memories = [_mem(i, importance=0.95) for i in range(1, 41)]
    ops = [{"op": "DELETE", "id": i, "reason": "x"} for i in range(1, 21)]

    _, refusal = decide_ops(memories, ops, max_ops_pct=0.25, min_age_hours=24)

    assert refusal == "quota_exceeded_pct"


def test_decide_ops_add_only_plan_is_not_destructive():
    """ADD creates no risk of losing a fact, so it is not measured against the
    destructive percentage — only against the absolute cap."""
    memories = [_mem(i) for i in range(1, 41)]
    ops = [{"op": "ADD", "content": f"new {i}", "importance": 0.5} for i in range(20)]

    decisions, refusal = decide_ops(memories, ops, max_ops_pct=0.25, min_age_hours=24)

    assert refusal is None
    assert all(d["apply"] for d in decisions)


def test_decide_ops_noop_never_applies():
    memories = [_mem(i) for i in range(1, 41)]
    decisions, refusal = decide_ops(
        memories, [{"op": "NOOP"}], max_ops_pct=0.25, min_age_hours=24
    )
    assert refusal is None
    assert decisions[0]["apply"] is False
    assert decisions[0]["skip_reason"] == "noop"
