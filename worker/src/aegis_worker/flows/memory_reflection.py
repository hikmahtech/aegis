"""MemoryReflectionFlow — nightly per-agent memory consolidation (Phase 4).

Two steps, in this order:

1. `consolidate_agent_memories` (opt-in via `consolidate`) — PROPOSES an
   ADD/UPDATE/DELETE/NOOP plan, logs every proposed op to
   `agent_memory_ops_log`, and applies it ONLY when `dry_run=False` here AND
   `AEGIS_MEMORY_CONSOLIDATION_APPLY_ENABLED=true` in the worker environment.
   Both default to off. A DELETE is a soft retire, never a row removal.
2. `prune_agent_memories` — the hard cap (oldest/lowest-importance beyond
   `keep`), which deliberately ignores soft-retired rows so that step 1's
   retirements are not hard-deleted by step 2 minutes later.

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
    # Gate 1 of 2. Setting this False on /admin/flows expresses the INTENT to
    # apply; the worker's AEGIS_MEMORY_CONSOLIDATION_APPLY_ENABLED env var must
    # also be true before a single row is written.
    dry_run: bool = True
    # Refuse a plan whose destructive ops exceed this fraction of the agent's
    # live rows. Lower = stricter; the absolute cap and the protected-row rules
    # are code constants an admin edit cannot widen.
    max_ops_pct: float = 0.25
    # Never rewrite or retire a row younger than this.
    min_age_hours: int = 24
    # Hard-delete rows soft-retired longer ago than this. 0 = never, which is
    # the shipped default: retired rows stay recoverable indefinitely.
    retire_grace_days: int = 0


@workflow.defn(name="MemoryReflectionFlow")
class MemoryReflectionFlow:
    @workflow.run
    async def run(self, input: MemoryReflectionInput) -> dict:
        result: dict = {}
        if input.consolidate:
            try:
                result["consolidation"] = await workflow.execute_activity(
                    "consolidate_agent_memories",
                    args=[
                        input.agent_id,
                        input.dry_run,
                        input.max_ops_pct,
                        input.min_age_hours,
                    ],
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
            args=[input.keep, input.retire_grace_days],
            start_to_close_timeout=_TIMEOUT,
            retry_policy=RETRY_ONCE,
        )
        result.update(prune)
        return result
