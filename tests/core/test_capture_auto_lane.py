"""B3 — `POST /api/admin/capture {kind:"auto"}` intent classifier.

Real Postgres: every assertion here is about WHERE the note landed (a
`knowledge_content` row vs the Todoist impl being called), never about the
classifier returning a string. A "the mock was called" test passes with the
route broken.

Isolation: every external_id is `zzb3-…`, so the cleanup deletes are scoped to
this file and cannot strip rows another test file seeded. `knowledge_chunks`
(no FK, just an index) is deleted before its `knowledge_content` parents.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.llm import LLMClient, LLMTruncationError
from aegis.services.knowledge import KnowledgeStore
from httpx import ASGITransport, AsyncClient

_TEST_REQUIRED_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "n8n_ui_url": "https://n8n.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
}

_DIM = 768
_PREFIX = "zzb3-"


@pytest.fixture
def settings():
    return Settings(**_TEST_REQUIRED_SETTINGS)


class _FakeLLM:
    """Deterministic embeddings + a scripted `think`.

    `think` returns whatever `response` is set to, or raises `raises`. It also
    records the kwargs it was called with so the tier/purpose/db_pool wiring
    can be asserted.
    """

    def __init__(self, response: str = "", raises: BaseException | None = None):
        self.response = response
        self.raises = raises
        self.calls: list[dict] = []

    async def embed(self, texts, model="nomic-embed-text"):
        vecs = []
        for t in texts:
            v = [0.0] * _DIM
            v[sum(ord(c) for c in t) % _DIM] = 1.0
            vecs.append(v)
        return vecs

    async def think(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return {
            "response": self.response,
            "model": kwargs.get("model", "test-model"),
            "prompt_tokens": 11,
            "completion_tokens": 22,
        }


class _RecordingLLM(LLMClient):
    """A REAL `LLMClient` with only the HTTP layer stubbed.

    The `llm_calls` row is written inside `LLMClient._record_call` (issue #106),
    so a hand-rolled fake with its own `think()` makes every recording assertion
    vacuous — it would pass whether or not the production choke point works.
    Everything from `think()` down is the shipping code path here; only
    `chat.completions.create` and `embed` are stubbed.
    """

    def __init__(self, db_pool, *, content: str = "", finish_reason: str = "stop"):
        super().__init__(base_url="http://litellm.invalid/v1", db_pool=db_pool)
        self.create_calls: list[dict] = []

        async def _create(**kwargs):
            self.create_calls.append(kwargs)
            usage = SimpleNamespace(prompt_tokens=11, completion_tokens=22)
            choice = SimpleNamespace(
                message=SimpleNamespace(content=content), finish_reason=finish_reason
            )
            return SimpleNamespace(choices=[choice], usage=usage)

        self._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
        )

    async def embed(self, texts, model="nomic-embed-text"):
        vecs = []
        for t in texts:
            v = [0.0] * _DIM
            v[sum(ord(c) for c in t) % _DIM] = 1.0
            vecs.append(v)
        return vecs


@pytest_asyncio.fixture(loop_scope="function")
async def make_client(settings, db_pool):
    """Factory: build an app whose `state.llm` is the supplied fake."""
    created: list[_FakeLLM] = []

    async def _make(llm: _FakeLLM | None):
        app = create_app(run_lifespan=False)
        app.state.db_pool = db_pool
        app.state.knowledge_connector = KnowledgeStore(
            db_pool=db_pool, llm=llm or _FakeLLM(), embedding_model="nomic-embed-text"
        )
        if llm is not None:
            app.state.llm = llm
            created.append(llm)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[verify_auth] = lambda: None
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    yield _make

    await db_pool.execute(
        "DELETE FROM knowledge_chunks WHERE content_id IN "
        "(SELECT content_id FROM knowledge_content WHERE url LIKE $1)",
        f"aegis://life_fact/{_PREFIX}%",
    )
    await db_pool.execute(
        "DELETE FROM knowledge_content WHERE url LIKE $1",
        f"aegis://life_fact/{_PREFIX}%",
    )
    await db_pool.execute(
        "DELETE FROM audit_log WHERE action = 'capture_classified' "
        "AND target_id LIKE $1",
        f"{_PREFIX}%",
    )
    await db_pool.execute("DELETE FROM llm_calls WHERE purpose = 'capture_classify'")


def _reply(lane: str, confidence: float = 0.95, reason: str = "because") -> str:
    return f'{{"lane": "{lane}", "confidence": {confidence}, "reason": "{reason}"}}'


async def _post(client, ext_id: str, text: str = "some spoken note"):
    return await client.post(
        "/api/admin/capture",
        json={
            "text": text,
            "source": "voice",
            "kind": "auto",
            "external_id": f"{_PREFIX}{ext_id}",
        },
    )


async def _life_fact_rows(db_pool, ext_id: str) -> list:
    return await db_pool.fetch(
        "SELECT source_type, title FROM knowledge_content WHERE url = $1",
        f"aegis://life_fact/{_PREFIX}{ext_id}",
    )


# --- lane routing -----------------------------------------------------------


async def test_auto_life_fact_lands_in_knowledge_store(make_client, db_pool, monkeypatch):
    """A `life_fact` verdict writes a knowledge row and never touches Todoist."""
    todoist_calls: list = []

    async def fake_capture(*a, **kw):
        todoist_calls.append(kw)
        return "TASK-SHOULD-NOT-HAPPEN"

    monkeypatch.setattr(
        "aegis.services.chat._capture_to_inbox_impl", fake_capture, raising=False
    )
    async with await make_client(_FakeLLM(_reply("life_fact"))) as client:
        r = await _post(client, "lf1", "my passport expires in March 2030")

    assert r.status_code == 200, r.text
    assert r.json()["lane"] == "life_fact"
    assert r.json()["task_ref"] is None
    assert todoist_calls == [], "life_fact verdict still called the Todoist impl"
    rows = await _life_fact_rows(db_pool, "lf1")
    assert len(rows) == 1, "no knowledge_content row for the life_fact verdict"
    assert rows[0]["source_type"] == "life_fact"
    assert rows[0]["title"] == "my passport expires in March 2030"


async def test_auto_task_lands_in_todoist(make_client, db_pool, monkeypatch):
    """A `task` verdict calls the Todoist impl and writes no knowledge row."""
    seen: dict = {}

    async def fake_capture(pool, source_tag, external_id, title, description):
        seen["title"] = title
        return "TASK-AUTO-1"

    monkeypatch.setattr(
        "aegis.services.chat._capture_to_inbox_impl", fake_capture, raising=False
    )
    async with await make_client(_FakeLLM(_reply("task"))) as client:
        r = await _post(client, "t1", "call the plumber tomorrow")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lane"] == "task"
    assert body["task_ref"] == "TASK-AUTO-1"
    assert body["content_id"] is None
    assert seen["title"] == "call the plumber tomorrow"
    assert await _life_fact_rows(db_pool, "t1") == []


async def test_auto_question_degrades_to_task(make_client, db_pool, monkeypatch):
    """`question` has no answering surface here — it must file, not vanish."""
    seen: dict = {}

    async def fake_capture(pool, source_tag, external_id, title, description):
        seen["title"] = title
        return "TASK-QUESTION"

    monkeypatch.setattr(
        "aegis.services.chat._capture_to_inbox_impl", fake_capture, raising=False
    )
    async with await make_client(_FakeLLM(_reply("question", 0.99))) as client:
        r = await _post(client, "q1", "what time does the tip close on Sundays")

    assert r.status_code == 200, r.text
    assert r.json()["lane"] == "task"
    assert seen["title"] == "what time does the tip close on Sundays"
    assert await _life_fact_rows(db_pool, "q1") == []


async def test_auto_observation_degrades_to_task(make_client, db_pool, monkeypatch):
    """`observation` must NOT be guessed into a numeric store — file it."""

    async def fake_capture(pool, source_tag, external_id, title, description):
        return "TASK-OBSERVATION"

    monkeypatch.setattr(
        "aegis.services.chat._capture_to_inbox_impl", fake_capture, raising=False
    )
    async with await make_client(_FakeLLM(_reply("observation", 0.99))) as client:
        r = await _post(client, "o1", "weighed 81.4 kilos this morning")

    assert r.status_code == 200, r.text
    assert r.json()["lane"] == "task"
    assert r.json()["task_ref"] == "TASK-OBSERVATION"
    assert await _life_fact_rows(db_pool, "o1") == []


# --- degradation ------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "llm"),
    [
        ("unparseable", _FakeLLM("I reckon this is definitely a life fact.")),
        ("truncated_json", _FakeLLM('{"lane": "life_fac')),
        ("unknown_label", _FakeLLM(_reply("memory"))),
        ("low_confidence", _FakeLLM(_reply("life_fact", 0.2))),
        ("wrong_shape", _FakeLLM('["life_fact"]')),
        ("truncation_error", _FakeLLM(raises=LLMTruncationError("budget gone"))),
        ("llm_exploded", _FakeLLM(raises=RuntimeError("proxy 500"))),
        ("no_llm_client", None),
    ],
)
async def test_bad_llm_response_degrades_to_task(
    case, llm, make_client, db_pool, monkeypatch
):
    """Every failure mode lands in the Inbox — never the knowledge store.

    Asserting WHERE it landed, not merely that the request returned 200: a
    200 with a life_fact row written on garbage input is the bug this guards.
    """
    refs: list = []

    async def fake_capture(pool, source_tag, external_id, title, description):
        refs.append(title)
        return f"TASK-{case}"

    monkeypatch.setattr(
        "aegis.services.chat._capture_to_inbox_impl", fake_capture, raising=False
    )
    async with await make_client(llm) as client:
        r = await _post(client, f"deg-{case}", "ambiguous mumbling")

    assert r.status_code == 200, r.text
    assert r.json()["lane"] == "task", f"{case} did not degrade to the task lane"
    assert r.json()["task_ref"] == f"TASK-{case}"
    assert refs == ["ambiguous mumbling"]
    assert await _life_fact_rows(db_pool, f"deg-{case}") == [], (
        f"{case} wrote a life fact into the knowledge store"
    )


# --- inspectability ---------------------------------------------------------


async def test_classification_is_recorded_in_llm_calls(make_client, db_pool, monkeypatch):
    """The successful classifier call is visible in llm_calls, on the balanced tier."""

    async def fake_capture(*a, **kw):
        return "TASK-LOGGED"

    monkeypatch.setattr(
        "aegis.services.chat._capture_to_inbox_impl", fake_capture, raising=False
    )
    await db_pool.execute("DELETE FROM llm_calls WHERE purpose = 'capture_classify'")
    llm = _RecordingLLM(db_pool, content=_reply("task"))
    async with await make_client(llm) as client:
        r = await _post(client, "llm1")
    assert r.status_code == 200, r.text

    rows = await db_pool.fetch(
        "SELECT model, status, input_tokens, output_tokens FROM llm_calls "
        "WHERE purpose = 'capture_classify'"
    )
    # Exactly one: zero means the choke point never fired, two means the call
    # site records again on top of it and every spend report is inflated.
    assert len(rows) == 1, f"expected exactly one classifier llm_calls row, got {len(rows)}"
    assert rows[0]["status"] == "success"
    assert rows[0]["input_tokens"] == 11
    assert rows[0]["output_tokens"] == 22
    # The session tier map resolves fast→gemma4:e2b, balanced→qwen3:14b,
    # smart→qwen3:32b, so this pins the classifier to the balanced tier.
    assert rows[0]["model"] == "qwen3:14b"
    assert len(llm.create_calls) == 1, "the classifier made more than one LLM call"


async def test_truncated_classification_is_recorded_in_llm_calls(
    make_client, db_pool, monkeypatch
):
    """A truncated classifier call still lands exactly one row.

    `think()` raises `LLMTruncationError` AFTER a real, billed upstream call, so
    the row has to come off that branch of the choke point — the classifier
    itself no longer records anything.
    """

    async def fake_capture(*a, **kw):
        return "TASK-TRUNC"

    monkeypatch.setattr(
        "aegis.services.chat._capture_to_inbox_impl", fake_capture, raising=False
    )
    await db_pool.execute("DELETE FROM llm_calls WHERE purpose = 'capture_classify'")
    llm = _RecordingLLM(db_pool, content="", finish_reason="length")
    async with await make_client(llm) as c:
        r = await _post(c, "llm2")
    assert r.status_code == 200, r.text

    rows = await db_pool.fetch(
        "SELECT status, error FROM llm_calls WHERE purpose = 'capture_classify'"
    )
    assert len(rows) == 1, "a truncated classifier call left no llm_calls row"
    assert rows[0]["status"] == "error"
    assert "truncated" in (rows[0]["error"] or "")


async def test_classification_writes_an_audit_row(make_client, db_pool, monkeypatch):
    """A wrong route must be diagnosable: lane, label, confidence, reason, text."""

    async def fake_capture(*a, **kw):
        return "TASK-AUDIT"

    monkeypatch.setattr(
        "aegis.services.chat._capture_to_inbox_impl", fake_capture, raising=False
    )
    llm = _FakeLLM(_reply("question", 0.81, "it asks when the tip closes"))
    async with await make_client(llm) as client:
        r = await _post(client, "aud1", "when does the tip close")
    assert r.status_code == 200, r.text

    row = await db_pool.fetchrow(
        "SELECT actor, details FROM audit_log WHERE action = 'capture_classified' "
        "AND target_id = $1",
        f"{_PREFIX}aud1",
    )
    assert row is not None, "no audit_log row for the classification"
    import json

    details = row["details"]
    if isinstance(details, str):
        details = json.loads(details)
    assert row["actor"] == "capture:voice"
    assert details["label"] == "question"
    assert details["lane"] == "task"
    assert details["degraded"] is True
    assert details["confidence"] == 0.81
    assert details["reason"] == "it asks when the tip closes"
    assert details["text"] == "when does the tip close"
