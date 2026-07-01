"""MemoryReflectionFlow — nightly per-agent memory consolidation (Phase 4).

For now this caps each agent's memory (prune oldest/lowest-importance beyond
`keep`). A future step can add an LLM ADD/UPDATE/DELETE/NOOP consolidation pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.shared.retry import RETRY_ONCE

_TIMEOUT = timedelta(minutes=5)


@dataclass
class MemoryReflectionInput:
    agent_id: str = "sebas"
    keep: int = 50


@workflow.defn(name="MemoryReflectionFlow")
class MemoryReflectionFlow:
    @workflow.run
    async def run(self, input: MemoryReflectionInput) -> dict:
        return await workflow.execute_activity(
            "prune_agent_memories",
            input.keep,
            start_to_close_timeout=_TIMEOUT,
            retry_policy=RETRY_ONCE,
        )
