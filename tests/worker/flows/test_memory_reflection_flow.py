"""MemoryReflectionFlow — consolidation runs first, and never blocks the cap."""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.memory_reflection import (
        MemoryReflectionFlow,
        MemoryReflectionInput,
    )

_calls: list[str] = []


def _make_stubs(consolidate_raises: bool):
    @activity.defn(name="consolidate_agent_memories")
    async def stub_consolidate(agent_id: str, dry_run: bool = True) -> dict:
        _calls.append(f"consolidate:{agent_id}:{dry_run}")
        if consolidate_raises:
            raise ApplicationError("refused", non_retryable=True)
        return {"status": "ok", "ops": [{"op": "NOOP"}], "applied": 0, "dry_run": dry_run}

    @activity.defn(name="prune_agent_memories")
    async def stub_prune(keep: int = 50) -> dict:
        _calls.append(f"prune:{keep}")
        return {"status": "ok", "pruned": 3, "agents": 1}

    return [stub_consolidate, stub_prune]


async def _run(input: MemoryReflectionInput, consolidate_raises: bool = False) -> dict:
    _calls.clear()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq-mem",
            workflows=[MemoryReflectionFlow],
            activities=_make_stubs(consolidate_raises),
        ),
    ):
        return await env.client.execute_workflow(
            MemoryReflectionFlow.run,
            input,
            id=f"mem-{consolidate_raises}-{input.consolidate}",
            task_queue="tq-mem",
        )


@pytest.mark.asyncio
async def test_consolidation_runs_before_prune():
    out = await _run(MemoryReflectionInput(agent_id="sebas", keep=7, consolidate=True))
    assert _calls == ["consolidate:sebas:True", "prune:7"]
    assert out["consolidation"]["status"] == "ok"
    assert out["pruned"] == 3


@pytest.mark.asyncio
async def test_refused_consolidation_still_prunes():
    """The activity refusing (as it does for dry_run=False) must not fail the
    nightly run — the cap still executes and its result is still returned."""
    out = await _run(
        MemoryReflectionInput(agent_id="sebas", keep=9, consolidate=True),
        consolidate_raises=True,
    )
    assert "prune:9" in _calls
    assert out["consolidation"]["status"] == "error"
    assert out["pruned"] == 3
    assert out["status"] == "ok"


@pytest.mark.asyncio
async def test_consolidation_off_by_default():
    out = await _run(MemoryReflectionInput(agent_id="sebas", keep=50))
    assert _calls == ["prune:50"]
    assert "consolidation" not in out
    assert out["pruned"] == 3
