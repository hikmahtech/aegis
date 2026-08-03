"""The memory consolidation pass must not mutate agent_memory in dry-run.

These rows are the user's own accumulated knowledge (mostly human-authored
corrections), so the load-bearing tests here are the destructive ones: seed
real rows, run the pass under every plausible config, and prove both that no
mutating SQL was issued and that the rows are byte-identical afterwards.

A4 gave the pass a real apply path, so this spy stopped being belt-and-braces
and became the primary safety net. Two changes to it (issue #201):

* the mutation regex now covers MERGE / TRUNCATE / COPY … FROM / DROP /
  ALTER as well as DELETE / UPDATE / INSERT. The old three-verb regex would
  have watched a `TRUNCATE agent_memory` go by;
* `acquire()` is no longer a hard AssertionError but a RECORDING PROXY.
  A4 applies plans inside a transaction, which needs a real connection —
  refusing `acquire` would have meant the apply path simply could not be
  spied on at all. The proxy records connection-level statements into the
  same list, so `mutations` sees strictly MORE than it used to.

`test_spy_detects_an_injected_write` proves the rig can actually fail.
"""

from __future__ import annotations

import json
import re

import pytest
import pytest_asyncio
from aegis_worker.activities.memory import MemoryActivities
from temporalio.testing import ActivityEnvironment

from tests.llm_stub import StubbedLLMClient

_AID = "zzcons-agent"
_OTHER = "zzcons-other"

# Any statement that could change an agent_memory row. `agent_memory(?!\w)`
# matches the table but NOT `agent_memory_ops_log`, which the pass writes to
# by design in every mode — see test_ops_log_writes_are_not_counted_as_mutations.
_MUTATION = re.compile(
    r"\b(delete|update|insert|merge|truncate|copy|drop|alter|replace)\b"
    r"[\s\S]*\bagent_memory(?!\w)",
    re.IGNORECASE,
)


class _SpyConn:
    """A connection whose statements land in the owning spy's list."""

    def __init__(self, conn, queries: list[str]):
        self._conn = conn
        self._queries = queries

    async def execute(self, query, *args, **kwargs):
        self._queries.append(query)
        return await self._conn.execute(query, *args, **kwargs)

    async def fetch(self, query, *args, **kwargs):
        self._queries.append(query)
        return await self._conn.fetch(query, *args, **kwargs)

    async def fetchval(self, query, *args, **kwargs):
        self._queries.append(query)
        return await self._conn.fetchval(query, *args, **kwargs)

    async def fetchrow(self, query, *args, **kwargs):
        self._queries.append(query)
        return await self._conn.fetchrow(query, *args, **kwargs)

    def transaction(self, *args, **kwargs):
        return self._conn.transaction(*args, **kwargs)


class _SpyAcquire:
    """Async context manager mirroring asyncpg's `pool.acquire()`."""

    def __init__(self, pool, queries: list[str]):
        self._ctx = pool.acquire()
        self._queries = queries

    async def __aenter__(self):
        return _SpyConn(await self._ctx.__aenter__(), self._queries)

    async def __aexit__(self, *exc):
        return await self._ctx.__aexit__(*exc)


class _SpyPool:
    """Records every statement the activity issues, then delegates."""

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
        return _SpyAcquire(self._pool, self.queries)

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


# Filler rows so the agent clears _MIN_MEMORIES_TO_APPLY (10) and the quota
# arithmetic is exercised for real. Below that floor every plan is refused
# wholesale, which is the subject of its own test in test_memory_a4_rails.py.
_FILLER = 12
_TOTAL = _FILLER + 2


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
    for n in range(_FILLER):
        await db_pool.execute(
            "INSERT INTO agent_memory (agent_id, content, importance, source) "
            "VALUES ($1,$2,0.5,'correction')",
            _AID,
            f"unrelated lesson {n}",
        )
    foreign_id = await db_pool.fetchval(
        "INSERT INTO agent_memory (agent_id, content, importance, source) "
        "VALUES ($1,'another agent''s lesson',0.6,'correction') RETURNING id",
        _OTHER,
    )
    yield {"pool": db_pool, "ids": ids, "foreign_id": foreign_id}
    for aid in (_AID, _OTHER):
        await db_pool.execute("DELETE FROM agent_memory_ops_log WHERE agent_id = $1", aid)
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
    assert len(before) == _TOTAL


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


async def test_non_dry_run_without_env_switch_changes_nothing(seeded):
    """A4 replaces A3's unconditional refusal with a two-key gate. This is the
    half that matters most: `dry_run=False` from the DB config, but the worker
    env switch off ⇒ the pass degrades to dry-run and writes nothing.

    Note it does NOT raise: failing the nightly run on a config/env mismatch
    would also stop the memory cap in step 2 of the flow.
    """
    pool, ids = seeded["pool"], seeded["ids"]
    before = await _snapshot(pool, _AID)
    spy = _SpyPool(pool)
    llm = _StubLLM(_merge_plan(ids))
    act = MemoryActivities(db_pool=spy, llm_client=llm, apply_enabled=False)

    out = await ActivityEnvironment().run(act.consolidate_agent_memories, _AID, False)

    assert out["status"] == "apply_disabled"
    assert out["dry_run"] is True  # the EFFECTIVE mode, not the requested one
    assert out["applied"] == 0
    assert spy.mutations == [], f"apply-disabled run issued mutating SQL: {spy.mutations}"
    assert await _snapshot(pool, _AID) == before


async def test_ops_log_writes_are_not_counted_as_mutations(seeded):
    """The spy must flag writes to `agent_memory` and ignore writes to
    `agent_memory_ops_log` — the pass writes the ledger in every mode, so a
    regex that confused the two would make the whole suite unfalsifiable in
    the other direction (permanently red).
    """
    pool, ids = seeded["pool"], seeded["ids"]
    spy = _SpyPool(pool)
    act = MemoryActivities(db_pool=spy, llm_client=_StubLLM(_merge_plan(ids)))

    await ActivityEnvironment().run(act.consolidate_agent_memories, _AID, True)

    ledger_writes = [q for q in spy.queries if "agent_memory_ops_log" in q and "INSERT" in q]
    assert ledger_writes, "dry-run must still write the ops ledger"
    assert spy.mutations == []
    # And the regex is not simply never matching: the same verb against the
    # real table IS caught.
    assert _MUTATION.search("INSERT INTO agent_memory (agent_id) VALUES ('x')")


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM agent_memory WHERE agent_id = 'x'",
        "UPDATE agent_memory SET content = 'x'",
        "INSERT INTO agent_memory (agent_id) VALUES ('x')",
        "TRUNCATE agent_memory",
        "TRUNCATE TABLE public.agent_memory CASCADE",
        "MERGE INTO agent_memory t USING s ON t.id = s.id",
        "DROP TABLE agent_memory",
        "ALTER TABLE agent_memory DROP COLUMN content",
        "COPY agent_memory FROM STDIN",
    ],
)
def test_mutation_regex_covers_every_destructive_verb(sql):
    """Issue #201: the A3 regex matched only DELETE/UPDATE/INSERT, so a
    `TRUNCATE agent_memory` would have sailed past the primary safety net."""
    assert _MUTATION.search(sql), f"spy would not have caught: {sql}"


def test_mutation_regex_ignores_the_ledger_table():
    assert not _MUTATION.search(
        "INSERT INTO agent_memory_ops_log (agent_id, op, memory_id) VALUES ($1,$2,$3)"
    )
    assert not _MUTATION.search("DELETE FROM agent_memory_ops_log WHERE agent_id = $1")


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
    """Real `LLMClient`, stubbed HTTP: the row is written inside
    `LLMClient._record_call` (issue #106), so a fake `think()` would record
    nothing and this assertion would be about the fake."""
    pool, ids = seeded["pool"], seeded["ids"]
    await pool.execute("DELETE FROM llm_calls WHERE purpose = 'memory_consolidation'")
    llm = StubbedLLMClient(db_pool=pool, content=_merge_plan(ids))
    act = MemoryActivities(db_pool=pool, llm_client=llm, model="stub-model")

    await ActivityEnvironment().run(act.consolidate_agent_memories, _AID, True)

    rows = await pool.fetch(
        "SELECT model, input_tokens, output_tokens, agent_id, status FROM llm_calls "
        "WHERE purpose = 'memory_consolidation'"
    )
    # Exactly one — a second row would double-count this call's spend.
    assert len(rows) == 1, f"expected one memory_consolidation row, got {len(rows)}"
    assert rows[0]["model"] == "stub-model"
    assert rows[0]["status"] == "success"
    assert (rows[0]["input_tokens"], rows[0]["output_tokens"]) == (11, 22)
    assert rows[0]["agent_id"] == _AID
    await pool.execute("DELETE FROM llm_calls WHERE purpose = 'memory_consolidation'")
