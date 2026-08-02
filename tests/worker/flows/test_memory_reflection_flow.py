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
    async def stub_consolidate(
        agent_id: str, dry_run: bool = True, max_ops_pct: float = 0.25, min_age_hours: int = 24
    ) -> dict:
        # The rails are recorded too: a flow that dropped max_ops_pct on the
        # floor would leave the operator's quota edit with no effect at all.
        _calls.append(f"consolidate:{agent_id}:{dry_run}:{max_ops_pct}:{min_age_hours}")
        if consolidate_raises:
            raise ApplicationError("refused", non_retryable=True)
        return {"status": "ok", "ops": [{"op": "NOOP"}], "applied": 0, "dry_run": dry_run}

    @activity.defn(name="prune_agent_memories")
    async def stub_prune(keep: int = 50, retire_grace_days: int = 0) -> dict:
        _calls.append(f"prune:{keep}:{retire_grace_days}")
        return {"status": "ok", "pruned": 3, "purged_retired": 0, "agents": 1}

    return [stub_consolidate, stub_prune]


_run_seq = 0


async def _run(input: MemoryReflectionInput, consolidate_raises: bool = False) -> dict:
    global _run_seq
    _run_seq += 1
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
            id=f"mem-{_run_seq}",
            task_queue="tq-mem",
        )


@pytest.mark.asyncio
async def test_consolidation_runs_before_prune():
    out = await _run(MemoryReflectionInput(agent_id="sebas", keep=7, consolidate=True))
    assert _calls == ["consolidate:sebas:True:0.25:24", "prune:7:0"]
    assert out["consolidation"]["status"] == "ok"
    assert out["pruned"] == 3


@pytest.mark.asyncio
async def test_every_rail_reaches_the_activities():
    """Non-default rails throughout, so a flow that silently dropped one — and
    left it on its dataclass default — is caught here rather than in prod."""
    out = await _run(
        MemoryReflectionInput(
            agent_id="sebas",
            keep=7,
            consolidate=True,
            dry_run=False,
            max_ops_pct=0.05,
            min_age_hours=96,
            retire_grace_days=30,
        )
    )
    assert _calls == ["consolidate:sebas:False:0.05:96", "prune:7:30"]
    assert out["pruned"] == 3


@pytest.mark.asyncio
async def test_refused_consolidation_still_prunes():
    """A failed or refused plan must not fail the nightly run — the cap still
    executes and its result is still returned."""
    out = await _run(
        MemoryReflectionInput(agent_id="sebas", keep=9, consolidate=True),
        consolidate_raises=True,
    )
    assert "prune:9:0" in _calls
    assert out["consolidation"]["status"] == "error"
    assert out["pruned"] == 3
    assert out["status"] == "ok"


@pytest.mark.asyncio
async def test_consolidation_off_by_default():
    out = await _run(MemoryReflectionInput(agent_id="sebas", keep=50))
    assert _calls == ["prune:50:0"]
    assert "consolidation" not in out
    assert out["pruned"] == 3


@pytest.mark.asyncio
async def test_flow_defaults_are_the_safe_ones():
    """Constructing the input with no arguments — what a legacy DB row with no
    A4 keys produces — must be observe-only, strictest quota, no hard purge."""
    input = MemoryReflectionInput()
    assert input.consolidate is False
    assert input.dry_run is True
    assert input.retire_grace_days == 0
