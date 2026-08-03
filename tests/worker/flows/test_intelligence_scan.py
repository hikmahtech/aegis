"""IntelligenceScanFlow tests."""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.intel_scan import (
        SearchSourceInput,
        SearchSourceResult,
    )
    from aegis_worker.flows.intelligence_scan import (
        IntelligenceScanFlow,
        IntelligenceScanInput,
    )


_calls: dict[str, list] = {"search": [], "dedup": [], "score": [], "ingest": [], "capture": []}


@activity.defn(name="search_source")
async def stub_search(inp: SearchSourceInput) -> SearchSourceResult:
    _calls["search"].append(inp.source)
    return SearchSourceResult(
        source=inp.source,
        items=[
            {"title": "A", "url": "https://x/a", "snippet": "", "source": inp.source},
            {"title": "B", "url": "https://x/b", "snippet": "", "source": inp.source},
            {"title": "C", "url": "https://x/c", "snippet": "", "source": inp.source},
        ],
    )


@activity.defn(name="dedup_items")
async def stub_dedup(items: list[dict]) -> list[dict]:
    _calls["dedup"].append(len(items))
    # Drop last item as "already in KG"
    return items[:-1]


@activity.defn(name="score_significance")
async def stub_score(items: list[dict], topics: list[dict]) -> list[dict]:
    _calls["score"].append((len(items), len(topics)))
    scored = []
    for i, it in enumerate(items):
        scored.append({**it, "significance": 5 if i == 0 else 3})
    return scored


@activity.defn(name="ingest_intelligence")
async def stub_ingest(items: list[dict]) -> dict:
    _calls["ingest"].append([it["title"] for it in items])
    return {"ingested": len(items)}


@activity.defn(name="capture_to_inbox")
async def stub_capture(
    source_tag: str, external_id: str, title: str, description: str | None = None
) -> str | None:
    _calls["capture"].append((source_tag, external_id, title))
    return f"task-{external_id}"


ALL_STUBS = [stub_search, stub_dedup, stub_score, stub_ingest, stub_capture]


def _reset():
    for v in _calls.values():
        v.clear()


@pytest.mark.asyncio
async def test_scan_dedups_scores_and_ingests_worthy():
    _reset()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[IntelligenceScanFlow],
            activities=ALL_STUBS,
        ),
    ):
        result = await env.client.execute_workflow(
            IntelligenceScanFlow.run,
            IntelligenceScanInput(
                agent_id="raphael",
                source="hn",
                topics=["ai", "rust"],
                significance_threshold=4,
            ),
            id="is-1",
            task_queue="tq",
        )
    assert result["source"] == "hn"
    assert result["raw"] == 3
    assert result["novel"] == 2  # dedup dropped one
    assert result["ingested"] == 1  # only item[0] scored 5, item[1] scored 3
    assert _calls["ingest"] == [["A"]]


@pytest.mark.asyncio
async def test_scan_no_topics_returns_empty():
    _reset()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[IntelligenceScanFlow],
            activities=ALL_STUBS,
        ),
    ):
        result = await env.client.execute_workflow(
            IntelligenceScanFlow.run,
            IntelligenceScanInput(agent_id="raphael", source="hn", topics=[]),
            id="is-2",
            task_queue="tq",
        )
    assert result["raw"] == 0
    assert result["ingested"] == 0
    assert _calls["search"] == []  # no topics → skip search


@pytest.mark.asyncio
async def test_scan_degrades_when_score_fails():
    """A hung/erroring score_significance (gpt-oss:20b intermittently hangs past
    its 180s ceiling under LiteLLM proxy load) must NOT hard-fail the whole scan —
    the flow degrades to a clean completion with 0 ingested instead of letting the
    ActivityError fail the workflow. Same graceful-degrade pattern as
    alert_investigation's assess guard (PR #282)."""
    _reset()

    @activity.defn(name="score_significance")
    async def failing_score(items: list[dict], topics: list[dict]) -> list[dict]:
        _calls["score"].append("raised")
        raise RuntimeError("score_significance timed out")

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[IntelligenceScanFlow],
            activities=[stub_search, stub_dedup, failing_score, stub_ingest, stub_capture],
        ),
    ):
        # Must NOT raise — the workflow completes (degraded) rather than failing.
        result = await env.client.execute_workflow(
            IntelligenceScanFlow.run,
            IntelligenceScanInput(source="hn", topics=["ai"], significance_threshold=4),
            id="is-degrade",
            task_queue="tq",
        )
    assert result["source"] == "hn"
    assert result["raw"] == 3
    assert result["novel"] == 2
    assert result["ingested"] == 0
    assert result.get("score_degraded") is True
    assert _calls["ingest"] == []  # nothing ingested when scoring is unavailable


@pytest.mark.asyncio
async def test_scan_degrades_when_dedup_fails_and_still_ingests():
    """issue #136: a failing/timing-out dedup_items must degrade to "treat
    everything as novel", NOT fail the whole scan.

    The 9 timed-out prod runs (duration_ms 186-189k on 07-16/17/21) are
    dedup's 60s ceiling x ACT_RETRY's 3 attempts (60+1+60+2+60 = 183s) plus
    the search. dedup is a best-effort filter — its own per-item handler
    already treats an unreachable store as "novel" — so the flow must reach
    the same conclusion when the whole activity dies.

    The load-bearing assertions are the ones about what SURVIVED: scoring saw
    all 3 raw items (not 2, which is what stub_dedup would have produced) and
    the worthy item was actually ingested. A test that only asserted "no
    exception escaped" would pass on a flow that silently returned nothing.
    """
    _reset()

    @activity.defn(name="dedup_items")
    async def failing_dedup(items: list[dict]) -> list[dict]:
        _calls["dedup"].append("raised")
        raise RuntimeError("dedup_items timed out against a slow knowledge store")

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[IntelligenceScanFlow],
            activities=[stub_search, failing_dedup, stub_score, stub_ingest, stub_capture],
        ),
    ):
        # Must NOT raise — the workflow completes (degraded) rather than failing.
        result = await env.client.execute_workflow(
            IntelligenceScanFlow.run,
            IntelligenceScanInput(source="hn", topics=["ai"], significance_threshold=4),
            id="is-dedup-degrade",
            task_queue="tq",
        )
    assert result.get("dedup_degraded") is True
    assert result["raw"] == 3
    # Fallback is "everything is novel" — 3, not stub_dedup's 2.
    assert result["novel"] == 3
    # The steps AFTER the failed one still ran and produced real output.
    assert _calls["score"] == [(3, 1)], "scoring did not receive the un-deduped items"
    assert _calls["ingest"] == [["A"]], "the worthy item was not ingested"
    assert result["ingested"] == 1


@pytest.mark.asyncio
async def test_scan_surfaces_partial_search_degradation():
    """A search that lost some topics must be distinguishable from a quiet one:
    the flow propagates search_degraded + failed_topics into result_summary
    while still processing the topics that DID return."""
    _reset()

    @activity.defn(name="search_source")
    async def partial_search(inp: SearchSourceInput) -> SearchSourceResult:
        _calls["search"].append(inp.source)
        return SearchSourceResult(
            source=inp.source,
            items=[
                {"title": "A", "url": "https://x/a", "snippet": "", "source": inp.source},
                {"title": "B", "url": "https://x/b", "snippet": "", "source": inp.source},
            ],
            failed_topics=["rust"],
        )

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[IntelligenceScanFlow],
            activities=[partial_search, stub_dedup, stub_score, stub_ingest, stub_capture],
        ),
    ):
        result = await env.client.execute_workflow(
            IntelligenceScanFlow.run,
            IntelligenceScanInput(source="hn", topics=["ai", "rust"], significance_threshold=4),
            id="is-partial-search",
            task_queue="tq",
        )
    assert result.get("search_degraded") is True
    assert result.get("failed_topics") == ["rust"]
    # The surviving topic's items still went all the way through to ingestion —
    # the flags ride along on the normal success return, not on a bail-out.
    assert result["raw"] == 2
    assert result["novel"] == 1
    assert result["ingested"] == 1
    assert _calls["ingest"] == [["A"]]


@pytest.mark.asyncio
async def test_scan_all_deduped():
    _reset()

    @activity.defn(name="dedup_items")
    async def empty_dedup(items: list[dict]) -> list[dict]:
        return []

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[IntelligenceScanFlow],
            activities=[stub_search, empty_dedup, stub_score, stub_ingest],
        ),
    ):
        result = await env.client.execute_workflow(
            IntelligenceScanFlow.run,
            IntelligenceScanInput(source="hn", topics=["ai"]),
            id="is-3",
            task_queue="tq",
        )
    assert result["raw"] == 3
    assert result["novel"] == 0
    assert result["ingested"] == 0
