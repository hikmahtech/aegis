"""A3 — the memory consolidation PLANNER must never mutate agent_memory.

These rows are the user's own accumulated knowledge (mostly human-authored
corrections) and a wrong DELETE is unrecoverable, so the load-bearing tests
here are the destructive ones: seed real rows, run the pass under every
plausible config, and prove both that no mutating SQL was issued and that the
rows are byte-identical afterwards.
"""

from __future__ import annotations

import json
import re

import pytest
import pytest_asyncio
from aegis_worker.activities.memory import MemoryActivities
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

_AID = "zzcons-agent"
_OTHER = "zzcons-other"

# Any statement that could change an agent_memory row.
_MUTATION = re.compile(r"\b(delete|update|insert)\b[\s\S]*\bagent_memory\b", re.IGNORECASE)


class _SpyPool:
    """Records every statement the activity issues, then delegates.

    `acquire` is deliberately unavailable: a raw connection would let a future
    edit slip mutating SQL past this spy.
    """

    def __init__(self, pool):
        self._pool = pool
        self.queries: list[str] = []

    async def fetch(self, query, *args, **kwargs):
        self.queries.append(query)
        return await self._pool.fetch(query, *args, **kwargs)

    async def fetchrow(self, query, *args, **kwargs):
        self.queries.append(query)
        return await self._pool.fetchrow(query, *args, **kwargs)

    async def fetchval(self, query, *args, **kwargs):
        self.queries.append(query)
        return await self._pool.fetchval(query, *args, **kwargs)

    async def execute(self, query, *args, **kwargs):
        self.queries.append(query)
        return await self._pool.execute(query, *args, **kwargs)

    def acquire(self, *args, **kwargs):
        raise AssertionError("consolidation must not take a raw connection")

    @property
    def mutations(self) -> list[str]:
        return [q for q in self.queries if _MUTATION.search(q)]


class _StubLLM:
    def __init__(self, response: str = "[]", exc: Exception | None = None):
        self.response = response
        self.exc = exc
        self.calls: list[dict] = []

    async def think(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return {
            "response": self.response,
            "model": "stub-model",
            "prompt_tokens": 11,
            "completion_tokens": 22,
        }


async def _snapshot(pool, agent_id: str) -> list[tuple]:
    rows = await pool.fetch(
        "SELECT id, agent_id, content, importance, source, created_at FROM agent_memory "
        "WHERE agent_id = $1 ORDER BY id",
        agent_id,
    )
    return [tuple(r) for r in rows]


@pytest_asyncio.fixture(loop_scope="function")
async def seeded(db_pool):
    for aid in (_AID, _OTHER):
        await db_pool.execute("DELETE FROM agent_memory WHERE agent_id = $1", aid)
        await db_pool.execute("DELETE FROM llm_calls WHERE agent_id = $1", aid)
        await db_pool.execute("DELETE FROM agents WHERE id = $1", aid)
        await db_pool.execute(
            "INSERT INTO agents (id, name, role, system_prompt_path, active) "
            "VALUES ($1, 'Z', 'tester', '', true)",
            aid,
        )
    ids = []
    for content in (
        "The human prefers PRs opened against main, not master.",
        "The human wants PRs based on main branch.",
    ):
        ids.append(
            await db_pool.fetchval(
                "INSERT INTO agent_memory (agent_id, content, importance, source) "
                "VALUES ($1,$2,0.7,'interaction_correction') RETURNING id",
                _AID,
                content,
            )
        )
    foreign_id = await db_pool.fetchval(
        "INSERT INTO agent_memory (agent_id, content, importance, source) "
        "VALUES ($1,'another agent''s lesson',0.6,'correction') RETURNING id",
        _OTHER,
    )
    yield {"pool": db_pool, "ids": ids, "foreign_id": foreign_id}
    for aid in (_AID, _OTHER):
        await db_pool.execute("DELETE FROM agent_memory WHERE agent_id = $1", aid)
        await db_pool.execute("DELETE FROM llm_calls WHERE agent_id = $1", aid)
        await db_pool.execute("DELETE FROM agents WHERE id = $1", aid)


def _merge_plan(ids: list[int]) -> str:
    return json.dumps(
        [
            {
                "op": "UPDATE",
                "id": ids[0],
                "content": "The human prefers PRs opened against main.",
                "importance": 0.8,
            },
            {"op": "DELETE", "id": ids[1], "reason": "duplicate of the merged row"},
        ]
    )


# --------------------------------------------------------------------------
# The destructive tests — the point of A3.
# --------------------------------------------------------------------------


async def test_delete_plan_deletes_nothing(seeded):
    """A plan proposing a merge + a DELETE leaves the table byte-identical."""
    pool, ids = seeded["pool"], seeded["ids"]
    before = await _snapshot(pool, _AID)
    spy = _SpyPool(pool)
    act = MemoryActivities(db_pool=spy, llm_client=_StubLLM(_merge_plan(ids)))

    out = await ActivityEnvironment().run(act.consolidate_agent_memories, _AID, True)

    assert out["status"] == "ok"
    assert out["applied"] == 0
    ops = {(o["op"], o.get("id")) for o in out["ops"]}
    assert ("UPDATE", ids[0]) in ops
    assert ("DELETE", ids[1]) in ops
    merged = next(o for o in out["ops"] if o["op"] == "UPDATE")["content"]
    assert merged == "The human prefers PRs opened against main."

    assert spy.mutations == [], f"consolidation issued mutating SQL: {spy.mutations}"
    assert await _snapshot(pool, _AID) == before
    assert len(before) == 2


@pytest.mark.parametrize(
    "label,make_llm",
    [
        ("delete_heavy", lambda i: _StubLLM(json.dumps([{"op": "DELETE", "id": i}]))),
        ("merge_plan", lambda i: _StubLLM(_merge_plan([i, i]))),
        ("truncated", lambda i: _StubLLM(json.dumps([{"op": "DELETE", "id": i}])[:-3])),
        ("prose", lambda i: _StubLLM("Sure! I would delete everything.")),
        ("empty", lambda i: _StubLLM("")),
        ("raises", lambda i: _StubLLM(exc=RuntimeError("proxy down"))),
        ("no_llm", lambda i: None),
    ],
)
async def test_no_destructive_sql_under_any_plan(seeded, label, make_llm):
    """No LLM response shape — well-formed, truncated, prose, absent or
    exploding — produces mutating SQL against agent_memory."""
    pool, ids = seeded["pool"], seeded["ids"]
    before = await _snapshot(pool, _AID)
    llm = make_llm(ids[0])
    spy = _SpyPool(pool)
    act = MemoryActivities(db_pool=spy, llm_client=llm)

    await ActivityEnvironment().run(act.consolidate_agent_memories, _AID, True)

    assert spy.mutations == [], f"[{label}] issued mutating SQL: {spy.mutations}"
    assert await _snapshot(pool, _AID) == before, f"[{label}] rows changed"


async def test_non_dry_run_is_refused_and_changes_nothing(seeded):
    """dry_run=False is refused non-retryably — the refusal is unconditional,
    so no config, env var or DB state reaches an apply path."""
    pool, ids = seeded["pool"], seeded["ids"]
    before = await _snapshot(pool, _AID)
    spy = _SpyPool(pool)
    llm = _StubLLM(_merge_plan(ids))
    act = MemoryActivities(db_pool=spy, llm_client=llm)

    with pytest.raises(ApplicationError) as exc:
        await ActivityEnvironment().run(act.consolidate_agent_memories, _AID, False)

    assert exc.value.non_retryable is True
    assert "non_dry_run_refused" in str(exc.value)
    assert "agent_memory_ops_log" in str(exc.value)  # names what A4 must add
    assert llm.calls == []  # refused before spending a token
    assert spy.queries == []
    assert await _snapshot(pool, _AID) == before


async def test_apply_consolidation_does_not_exist_yet(seeded):
    """The structural half of the gate: there is no apply path to call, so
    deleting the refusal alone still cannot write. A4 adds this function."""
    import aegis.services.memory as memory_service

    assert not hasattr(memory_service, "apply_consolidation")


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


async def test_op_on_another_agents_memory_is_skipped(seeded):
    pool, foreign_id = seeded["pool"], seeded["foreign_id"]
    before_foreign = await _snapshot(pool, _OTHER)
    llm = _StubLLM(json.dumps([{"op": "DELETE", "id": foreign_id, "reason": "not yours"}]))
    act = MemoryActivities(db_pool=pool, llm_client=llm)

    out = await ActivityEnvironment().run(act.consolidate_agent_memories, _AID, True)

    assert out["ops"] == []
    assert out["skipped"] == 1
    assert await _snapshot(pool, _OTHER) == before_foreign


async def test_malformed_response_is_never_a_delete_plan(seeded):
    """A truncated response must collapse to an empty plan, not a delete list."""
    pool, ids = seeded["pool"], seeded["ids"]
    # Cut off mid-string, exactly as a max_tokens stop does.
    truncated = json.dumps([{"op": "DELETE", "id": ids[0], "reason": "dup"}])[:-3]
    assert truncated.endswith('"reason": "dup')
    llm = _StubLLM(truncated)
    act = MemoryActivities(db_pool=pool, llm_client=llm)

    out = await ActivityEnvironment().run(act.consolidate_agent_memories, _AID, True)

    assert out["ops"] == []
    assert out["status"] == "unparseable"


async def test_junk_ops_are_counted_not_executed(seeded):
    pool, ids = seeded["pool"], seeded["ids"]
    llm = _StubLLM(
        json.dumps(
            [
                {"op": "DROP TABLE", "id": ids[0]},
                {"op": "DELETE", "id": 99999999},
                {"op": "DELETE", "id": True},
                {"op": "UPDATE", "id": ids[0], "content": "   "},
                {"op": "ADD", "content": ""},
                "not-a-dict",
                {"op": "NOOP"},
                {"op": "ADD", "content": "a real generalisation", "importance": "banana"},
            ]
        )
    )
    act = MemoryActivities(db_pool=pool, llm_client=llm)

    out = await ActivityEnvironment().run(act.consolidate_agent_memories, _AID, True)

    assert out["skipped"] == 6
    assert [o["op"] for o in out["ops"]] == ["NOOP", "ADD"]
    assert out["ops"][1]["importance"] == 0.5  # unparseable importance → default


async def test_llm_failure_returns_llm_failed(seeded):
    pool = seeded["pool"]
    act = MemoryActivities(db_pool=pool, llm_client=_StubLLM(exc=RuntimeError("proxy down")))

    out = await ActivityEnvironment().run(act.consolidate_agent_memories, _AID, True)

    assert out["status"] == "llm_failed"
    assert out["ops"] == []


async def test_no_memories_skips_before_the_llm(seeded):
    pool = seeded["pool"]
    await pool.execute("DELETE FROM agent_memory WHERE agent_id = $1", _AID)
    llm = _StubLLM(_merge_plan([1, 2]))
    act = MemoryActivities(db_pool=pool, llm_client=llm)

    out = await ActivityEnvironment().run(act.consolidate_agent_memories, _AID, True)

    assert out == {
        "applied": 0,
        "dry_run": True,
        "ops": [],
        "skipped": 0,
        "status": "skipped",
        "reason": "no_memories",
    }
    assert llm.calls == []


# --------------------------------------------------------------------------
# Observability (#106): the call must land in llm_calls.
# --------------------------------------------------------------------------


async def test_llm_call_is_logged_with_purpose(seeded):
    pool, ids = seeded["pool"], seeded["ids"]
    await pool.execute("DELETE FROM llm_calls WHERE purpose = 'memory_consolidation'")
    llm = _StubLLM(_merge_plan(ids))
    act = MemoryActivities(db_pool=pool, llm_client=llm)

    await ActivityEnvironment().run(act.consolidate_agent_memories, _AID, True)

    assert llm.calls[0]["purpose"] == "memory_consolidation"
    assert llm.calls[0]["db_pool"] is pool  # failures self-log inside think()
    row = await pool.fetchrow(
        "SELECT model, input_tokens, output_tokens, agent_id FROM llm_calls "
        "WHERE purpose = 'memory_consolidation' ORDER BY created_at DESC LIMIT 1"
    )
    assert row is not None, "no llm_calls row written for memory_consolidation"
    assert row["model"] == "stub-model"
    assert (row["input_tokens"], row["output_tokens"]) == (11, 22)
    assert row["agent_id"] == _AID
    await pool.execute("DELETE FROM llm_calls WHERE purpose = 'memory_consolidation'")
