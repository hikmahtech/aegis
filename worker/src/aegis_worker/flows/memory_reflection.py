"""MemoryReflectionFlow — nightly per-agent memory consolidation (Phase 4).

Two steps, in this order:

1. `consolidate_agent_memories` (opt-in via `consolidate`) — PROPOSES an
   ADD/UPDATE/DELETE/NOOP plan and returns it for inspection. It never writes;
   `dry_run=False` is refused by the activity until A4's safety rails land.
   Running it first means that once A4 does land, the cap deletes fewer real
   lessons.
2. `prune_agent_memories` — the hard cap (oldest/lowest-importance beyond
   `keep`). This is the only step that deletes anything today.

Step 1 has its own try/except: a failed or refused plan must still leave the
cap running.
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
    consolidate: bool = False
    # Observe-only. The activity refuses False outright — this field exists so
    # A4 can flip it once the rails exist, not so it can be flipped today.
    dry_run: bool = True


@workflow.defn(name="MemoryReflectionFlow")
class MemoryReflectionFlow:
    @workflow.run
    async def run(self, input: MemoryReflectionInput) -> dict:
        result: dict = {}
        if input.consolidate:
            try:
                result["consolidation"] = await workflow.execute_activity(
                    "consolidate_agent_memories",
                    args=[input.agent_id, input.dry_run],
                    start_to_close_timeout=_TIMEOUT,
                    retry_policy=RETRY_ONCE,
                )
            except Exception as exc:  # noqa: BLE001 — a failed plan must still let the cap run
                workflow.logger.warning(
                    "memory_consolidation_step_failed agent=%s err=%s",
                    input.agent_id,
                    str(exc)[:200],
                )
                result["consolidation"] = {"status": "error", "error": str(exc)[:200]}

        prune = await workflow.execute_activity(
            "prune_agent_memories",
            input.keep,
            start_to_close_timeout=_TIMEOUT,
            retry_policy=RETRY_ONCE,
        )
        result.update(prune)
        return result
