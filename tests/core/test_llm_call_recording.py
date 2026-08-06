"""Issue #106 — `LLMClient` is the single writer of `llm_calls` rows.

Before this, `think()` recorded only failures and raised `LLMTruncationError`
from OUTSIDE its own recording try, so a successful or truncated call was
invisible unless the call site remembered to write the row itself. Several
never did (weekly review, daily briefing, the admin ad-hoc routes), and their
spend simply did not exist as far as the governor was concerned.

Two properties are load-bearing here and neither can be checked with a mock:

* **exactly one row** — zero means the choke point never fired, two means a
  call site records on top of it and every spend report is silently inflated;
* **the row is really in Postgres** — `record_llm_call` swallows its own
  errors, so an `execute` mock passes against a write that never landed.

So every test drives a real `LLMClient` (only `chat.completions.create` is
stubbed) against the real-Postgres `db_pool` and reads the rows back. Purposes
are prefixed `zz106-` so the cleanup can't touch another file's rows.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
import structlog
from aegis.llm import LLMTruncationError

from tests.llm_stub import StubbedLLMClient

_PREFIX = "zz106-"
_AGENT = "zz106-agent"


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    """Real pool plus a prefix-scoped agent row for `llm_calls.agent_id`'s FK.

    Teardown is children-before-parents: llm_calls rows first, then the agent.
    """
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, active) "
        "VALUES ($1,$1,'test','/dev/null',true) ON CONFLICT (id) DO NOTHING",
        _AGENT,
    )
    await db_pool.execute("DELETE FROM llm_calls WHERE purpose LIKE $1", f"{_PREFIX}%")
    try:
        yield db_pool
    finally:
        await db_pool.execute("DELETE FROM llm_calls WHERE purpose LIKE $1", f"{_PREFIX}%")
        await db_pool.execute("DELETE FROM agents WHERE id = $1", _AGENT)


async def _rows(pool, purpose: str) -> list:
    return await pool.fetch(
        "SELECT model, purpose, agent_id, input_tokens, output_tokens, latency_ms, "
        "status, error FROM llm_calls WHERE purpose = $1",
        purpose,
    )


# --------------------------------------------------------------- three outcomes


async def test_a_successful_think_writes_exactly_one_row(pool):
    purpose = f"{_PREFIX}think-ok"
    client = StubbedLLMClient(db_pool=pool, content="hello", prompt_tokens=7, completion_tokens=3)

    await client.think("go", model="gemma4:e2b", db_pool=pool, purpose=purpose, agent_id=_AGENT)

    rows = await _rows(pool, purpose)
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
    assert rows[0]["status"] == "success"
    assert rows[0]["model"] == "gemma4:e2b"
    assert rows[0]["agent_id"] == _AGENT
    assert rows[0]["input_tokens"] == 7
    assert rows[0]["output_tokens"] == 3
    assert rows[0]["error"] is None


async def test_a_truncated_think_writes_one_error_row(pool):
    """The truncation branch sits after a real, billed upstream call — it used
    to be outside the recording try, which is precisely the bug."""
    purpose = f"{_PREFIX}think-truncated"
    client = StubbedLLMClient(
        db_pool=pool, content="", finish_reason="length", prompt_tokens=50, completion_tokens=256
    )

    with pytest.raises(LLMTruncationError):
        await client.think(
            "go", model="gpt-oss:20b", db_pool=pool, purpose=purpose, agent_id=_AGENT
        )

    rows = await _rows(pool, purpose)
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
    assert rows[0]["status"] == "error"
    assert (rows[0]["error"] or "").startswith("truncated: ")
    # The tokens were spent even though no visible content came back.
    assert rows[0]["input_tokens"] == 50
    assert rows[0]["output_tokens"] == 256


async def test_a_clipped_think_records_clipped_not_success(pool):
    """finish_reason=length WITH visible content is the other truncation: the
    response was cut mid-write. It must not be raised (partial content is often
    usable) and must not be filed as a plain success — prod ran 6/42
    intel_score_significance calls into this and every one counted as healthy.
    """
    purpose = f"{_PREFIX}think-clipped"
    client = StubbedLLMClient(
        db_pool=pool,
        content='[{"topic": "half an ans',
        finish_reason="length",
        prompt_tokens=40,
        completion_tokens=2048,
    )

    # Deliberately does NOT raise — that is the behavioural contract.
    result = await client.think(
        "go", model="kimi-k2.5", db_pool=pool, purpose=purpose, agent_id=_AGENT
    )
    assert result["response"] == '[{"topic": "half an ans'

    rows = await _rows(pool, purpose)
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
    assert rows[0]["status"] == "clipped", "a clipped call must not hide in the success count"
    assert (rows[0]["error"] or "").startswith("clipped: ")
    assert rows[0]["output_tokens"] == 2048


async def test_an_uncut_think_is_still_plain_success(pool):
    """Guard the other direction: normal completions must keep status=success,
    or every dashboard that counts successes breaks."""
    purpose = f"{_PREFIX}think-stop"
    client = StubbedLLMClient(
        db_pool=pool, content="all done", finish_reason="stop", prompt_tokens=5, completion_tokens=2
    )

    await client.think("go", model="kimi-k2.5", db_pool=pool, purpose=purpose, agent_id=_AGENT)

    rows = await _rows(pool, purpose)
    assert rows[0]["status"] == "success"
    assert rows[0]["error"] is None


async def test_a_failed_think_still_writes_one_row(pool):
    purpose = f"{_PREFIX}think-failed"

    class _UpstreamError(RuntimeError):
        pass

    client = StubbedLLMClient(db_pool=pool, raises=_UpstreamError("read timed out"))

    with pytest.raises(_UpstreamError):
        await client.think(
            "go", model="gemma4:e2b", db_pool=pool, purpose=purpose, agent_id=_AGENT
        )

    rows = await _rows(pool, purpose)
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
    assert rows[0]["status"] == "timeout"  # classified off the message
    assert "read timed out" in (rows[0]["error"] or "")


async def test_a_successful_chat_writes_exactly_one_row(pool):
    """`chat()` is the other generation entry point and had no success row at
    all before #106 — only `services/chat.py` recorded, by hand."""
    purpose = f"{_PREFIX}chat-ok"
    client = StubbedLLMClient(db_pool=pool, content="hi", prompt_tokens=4, completion_tokens=2)

    result = await client.chat(
        [{"role": "user", "content": "hi"}],
        model="qwen3:14b",
        db_pool=pool,
        purpose=purpose,
        agent_id=_AGENT,
    )

    assert result["response"] == "hi"
    rows = await _rows(pool, purpose)
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
    assert rows[0]["status"] == "success"
    assert rows[0]["model"] == "qwen3:14b"
    assert rows[0]["input_tokens"] == 4


async def test_a_failed_chat_writes_one_row(pool):
    purpose = f"{_PREFIX}chat-failed"

    class _UpstreamError(RuntimeError):
        pass

    client = StubbedLLMClient(db_pool=pool, raises=_UpstreamError("bad gateway"))

    with pytest.raises(_UpstreamError):
        await client.chat(
            [{"role": "user", "content": "hi"}],
            model="qwen3:14b",
            db_pool=pool,
            purpose=purpose,
            agent_id=_AGENT,
        )

    rows = await _rows(pool, purpose)
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
    assert rows[0]["status"] == "error"


# ------------------------------------------------------------- pool resolution


async def test_a_governed_client_records_without_a_per_call_pool(pool):
    """The client's own pool is enough — `purpose` alone makes a row.

    This is what closes the admin ad-hoc routes named in #106
    (`routes/agents.py::draft_persona`, `routes/todoist.py`): they hold
    `app.state.llm`, which `api/app.py` builds with `db_pool=pool`.
    """
    purpose = f"{_PREFIX}client-pool"
    client = StubbedLLMClient(db_pool=pool, content="ok")

    await client.think("go", model="gemma4:e2b", purpose=purpose)

    rows = await _rows(pool, purpose)
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
    assert rows[0]["agent_id"] is None


async def test_an_ungoverned_client_records_nothing(pool):
    """`routes/llm_backend.py::test_backend` builds a client with NO pool on
    purpose, so it stays usable while the kill switch is active. It must not
    start writing rows — and must not crash reaching for a pool it never had."""
    purpose = f"{_PREFIX}ungoverned"
    client = StubbedLLMClient(db_pool=None, content="ok")

    result = await client.think("go", model="gemma4:e2b", purpose=purpose)

    assert result["response"] == "ok"
    assert await _rows(pool, purpose) == []


# ------------------------------------------------------------ the loud failure


async def test_a_pool_without_a_purpose_warns_instead_of_silently_skipping(pool):
    """A caller that wires a pool and forgets `purpose` has made a mistake.

    `purpose` is the only column that makes spend attributable, so there is no
    row to write — but dropping it silently is how #106 stayed open for weeks.
    structlog bypasses stdlib logging, so `caplog` sees nothing here; the log
    has to be captured through structlog's own testing helper.
    """
    client = StubbedLLMClient(db_pool=pool, content="ok")
    before = await pool.fetchval("SELECT count(*) FROM llm_calls")

    with structlog.testing.capture_logs() as logs:
        await client.think("go", model="gemma4:e2b", db_pool=pool)

    events = [entry for entry in logs if entry["event"] == "llm_call_unrecorded"]
    assert events, f"no llm_call_unrecorded warning; captured: {[e['event'] for e in logs]}"
    assert events[0]["log_level"] == "warning"
    assert events[0]["reason"] == "missing_purpose"
    assert await pool.fetchval("SELECT count(*) FROM llm_calls") == before


async def test_a_client_managing_its_own_recording_is_not_warned_about(pool):
    """`services/chat.py`'s tool loop passes neither `db_pool` nor `purpose` and
    records by hand. It is the highest-volume LLM path in AEGIS, so warning on
    every turn would be both noisy and wrong — and it must not double-log."""
    client = StubbedLLMClient(db_pool=pool, content="ok")
    before = await pool.fetchval("SELECT count(*) FROM llm_calls")

    with structlog.testing.capture_logs() as logs:
        await client.chat([{"role": "user", "content": "hi"}], model="qwen3:14b")

    assert [e for e in logs if e["event"] == "llm_call_unrecorded"] == []
    assert await pool.fetchval("SELECT count(*) FROM llm_calls") == before


# --------------------------------------------- the call sites named in #106
#
# Each of these wrote NO row at all before the choke point. They are asserted
# end to end, through the real route/service, because the whole failure mode
# was a call site that looked wired up and wasn't.


async def test_knowledge_ask_records_its_call(pool):
    """`KnowledgeStore.ask` already passed `db_pool` + `purpose`, so a failure
    recorded — but a SUCCESSFUL RAG answer wrote nothing. No change to
    `services/knowledge.py` was needed to fix that; the choke point covers it,
    and this test is what proves it."""
    from aegis.services.knowledge import KnowledgeStore

    vec = [0.0] * 768
    vec[0] = 1.0
    content_id = "zz106-know"
    await pool.execute(
        "INSERT INTO knowledge_content (content_id, url, title, source_type, tags) "
        "VALUES ($1,$2,'Homelab','article',ARRAY['zz106']) "
        "ON CONFLICT (content_id) DO NOTHING",
        content_id,
        f"aegis://{_PREFIX}know",
    )
    await pool.execute(
        "INSERT INTO knowledge_chunks (content_id, chunk_index, chunk_text, embedding) "
        "VALUES ($1, 0, 'The swarm has three nodes.', $2)",
        content_id,
        "[" + ",".join(str(x) for x in vec) + "]",
    )
    try:
        llm = StubbedLLMClient(db_pool=pool, content="Three nodes. [1]", embed_vector=vec)
        store = KnowledgeStore(db_pool=pool, llm=llm, embedding_model="nomic-embed-text")

        out = await store.ask("how many nodes?")

        assert out["answer"] == "Three nodes. [1]"
        rows = await _rows(pool, "knowledge_ask")
        assert len(rows) == 1, f"expected one knowledge_ask row, got {len(rows)}"
        assert rows[0]["status"] == "success"
    finally:
        await pool.execute("DELETE FROM knowledge_chunks WHERE content_id = $1", content_id)
        await pool.execute("DELETE FROM knowledge_content WHERE content_id = $1", content_id)
        await pool.execute("DELETE FROM llm_calls WHERE purpose = 'knowledge_ask'")


async def test_persona_draft_route_records_its_call(pool, test_settings):
    """`POST /api/agents/{id}/draft` passed a `purpose` but no pool, so not even
    its failures could record. It now also attributes to the agent it drafts."""
    import base64

    from aegis.api.app import create_app
    from aegis.api.deps import get_settings
    from httpx import ASGITransport, AsyncClient

    app = create_app(run_lifespan=False)
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.state.db_pool = pool
    app.state.llm = StubbedLLMClient(
        db_pool=pool,
        content='{"soul": "s", "operating_notes": "o", "user_context": "u"}',
    )
    headers = {"Authorization": "Basic " + base64.b64encode(b"admin:admin").decode()}

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.post(
                f"/api/agents/{_AGENT}/draft",
                headers=headers,
                json={"description": "a quiet archivist"},
            )
        assert r.status_code == 200, r.text
        assert r.json()["soul"] == "s"

        rows = await _rows(pool, "persona_draft")
        assert len(rows) == 1, f"expected one persona_draft row, got {len(rows)}"
        assert rows[0]["status"] == "success"
        assert rows[0]["agent_id"] == _AGENT
    finally:
        await pool.execute("DELETE FROM llm_calls WHERE purpose = 'persona_draft'")
