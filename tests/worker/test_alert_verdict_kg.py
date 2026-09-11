"""The verdict reaches the knowledge store tagged with what the human decided,
and recall prefers an approved fix and leaves a discarded one out (#502).

`record_verdict_to_kg` used to run before the Gate-2 card went out, so a
verdict the operator threw away was saved exactly like one they acted on, and
`gather_alert_knowledge` recalled both with the same weight. The recall tests
run the real `KnowledgeStore` on the test database with an embedder that maps
every related text to one vector, and the approved verdict to one slightly
less like the query: only its outcome can put it first.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis.services.knowledge import KnowledgeStore
from aegis_worker.activities.alerts import AlertActivities
from temporalio.testing import ActivityEnvironment

pytestmark = pytest.mark.asyncio

_DIM = 768


def _vec(text: str) -> list[float]:
    v = [0.0] * _DIM
    # A text marked UNRELATED is orthogonal to everything else.
    v[1 if "UNRELATED" in text else 0] = 1.0
    if "APPROVED" in text or "RAN" in text:
        # A little LESS like the query than the rest (cosine 0.96), so only
        # its outcome can put it first.
        v[2] = 0.3
    return v


class _OneVectorLLM:
    """The store's LLM. `think` is counted: recall must not need a model."""

    def __init__(self) -> None:
        self.thinks = 0

    async def embed(self, texts, model="nomic-embed-text"):
        return [_vec(t) for t in texts]

    async def think(self, prompt, **kwargs):
        self.thinks += 1
        return {"response": "synthesized", "model": "fake"}


@pytest_asyncio.fixture(loop_scope="function")
async def kg(db_pool):
    llm = _OneVectorLLM()
    store = KnowledgeStore(db_pool=db_pool, llm=llm, embedding_model="nomic-embed-text")
    marker = uuid.uuid4().hex[:8]
    yield store, llm, marker
    await db_pool.execute(
        "DELETE FROM knowledge_content WHERE url LIKE $1", f"aegis://alert/kgtest-{marker}%"
    )


def _alert(marker: str, n: str) -> dict:
    return {
        "title": f"Checkout timeouts {marker}",
        "fingerprint": f"kgtest-{marker}-{n}",
        "source": "sentry",
    }


_VERDICT = {"status": "actionable", "root_cause": "pool exhausted", "confidence": 0.9}


async def test_the_outcome_is_on_the_record():
    kc = AsyncMock()
    kc.ingest_content = AsyncMock(return_value={"status": "ok"})
    acts = AlertActivities(knowledge_connector=kc)
    alert = {"title": "OOM", "fingerprint": "fp-1", "source": "alertmanager"}

    out = await ActivityEnvironment().run(
        acts.record_verdict_to_kg, alert, _VERDICT, "transcript", "discarded"
    )

    assert out == {"ingested": True, "outcome": "discarded"}
    kw = kc.ingest_content.await_args.kwargs
    # One document per alert AND outcome: a later discard must not overwrite
    # the verdict the operator approved last week.
    assert kw["url"] == "aegis://alert/fp-1#discarded"
    assert kw["metadata"]["outcome"] == "discarded"
    assert "outcome:discarded" in kw["tags"]
    assert kw["source_type"] == "alert_investigation"


async def test_a_call_without_an_outcome_writes_what_it_always_did():
    """A run in flight across the deploy replays the old Step 7b, which passes
    three arguments. It must write the same document it always wrote."""
    kc = AsyncMock()
    kc.ingest_content = AsyncMock(return_value={"status": "ok"})
    acts = AlertActivities(knowledge_connector=kc)

    await ActivityEnvironment().run(
        acts.record_verdict_to_kg, {"title": "OOM", "fingerprint": "fp-1", "source": "x"}, _VERDICT, "t"
    )

    kw = kc.ingest_content.await_args.kwargs
    assert kw["url"] == "aegis://alert/fp-1"
    assert "outcome" not in kw["metadata"]
    assert not any(t.startswith("outcome:") for t in kw["tags"])


async def test_recall_puts_an_approved_fix_first_and_leaves_a_discarded_one_out(kg):
    store, llm, m = kg
    acts = AlertActivities(knowledge_connector=store)
    env = ActivityEnvironment()
    for n, outcome, text in [
        ("a", "no_card", "NO-CARD verdict: pool exhausted, nothing to decide"),
        ("b", "discarded", "DISCARDED verdict: raise the timeout to 90s"),
        ("c", "opened_pr", "APPROVED verdict: cap the pool and add a queue"),
        ("d", "", "LEGACY verdict from before outcomes were recorded"),
        ("e", "opened_pr", "UNRELATED verdict about a printer"),
    ]:
        await env.run(acts.record_verdict_to_kg, _alert(m, n), _VERDICT, text, outcome)

    recall = await env.run(acts.gather_alert_knowledge, f"Checkout timeouts {m}", "shop", "")

    assert "Prior incidents" in recall
    assert "APPROVED verdict" in recall
    assert "DISCARDED verdict" not in recall, "a discarded fix must not come back as advice"
    assert recall.index("APPROVED verdict") < recall.index("NO-CARD verdict")
    assert recall.index("APPROVED verdict") < recall.index("LEGACY verdict")
    assert "opened a fix PR" in recall
    assert "UNRELATED" not in recall, "below the similarity floor"
    assert llm.thinks == 0, "recall is a search, not a model call"


async def test_recall_shows_one_line_per_alert_its_best_outcome(kg):
    store, _, m = kg
    acts = AlertActivities(knowledge_connector=store)
    env = ActivityEnvironment()
    alert = _alert(m, "same")
    await env.run(acts.record_verdict_to_kg, alert, _VERDICT, "ACKED verdict of the same alert", "acknowledged")
    await env.run(acts.record_verdict_to_kg, alert, _VERDICT, "RAN verdict of the same alert", "run_fix")

    recall = await env.run(acts.gather_alert_knowledge, f"Checkout timeouts {m}", "", "")

    assert "RAN verdict" in recall and "ran the proposed commands" in recall
    assert "ACKED verdict" not in recall


async def test_recall_with_nothing_but_discarded_fixes_says_nothing(kg):
    store, _, m = kg
    acts = AlertActivities(knowledge_connector=store)
    env = ActivityEnvironment()
    await env.run(acts.record_verdict_to_kg, _alert(m, "x"), _VERDICT, "DISCARDED only", "discarded")

    assert await env.run(acts.gather_alert_knowledge, f"Checkout timeouts {m}", "", "") == ""


async def test_the_llm_fallback_no_longer_writes_the_store_before_the_decision():
    """`investigate()` used to ingest its own text the moment it ran — before
    any card — at the same address Step 7b then overwrote. With the write
    after the decision, that early copy would outlive a discard."""
    kc = AsyncMock()
    llm = AsyncMock()
    llm.think = AsyncMock(return_value={"response": "Root cause: a fix is needed", "model": "m"})
    acts = AlertActivities(llm_client=llm, knowledge_connector=kc)

    out = await ActivityEnvironment().run(
        acts.investigate, {"title": "t", "fingerprint": "fp", "source": "sentry"}, ""
    )

    assert out["investigation"] == "Root cause: a fix is needed"
    kc.ingest_content.assert_not_awaited()
