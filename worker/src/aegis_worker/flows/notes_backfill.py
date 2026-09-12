"""NotesBackfillFlow — the daylog's old entries into the vault journal, once (#514).

Started by hand, never scheduled:

    temporal workflow start --type NotesBackfillFlow --task-queue aegis-main \\
      --workflow-id notes-backfill-journal --input '{"agent_id": "raphael"}'

It writes the existing `daylog` / `daylog_rollup` knowledge rows into the
matching journal notes through the vault writer, with the markers the live
daylog uses — so a day already in the journal is left alone and running it
twice writes nothing the second time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.shared.retry import NO_RETRY

_BACKFILL_TIMEOUT = timedelta(minutes=30)


@dataclass
class NotesBackfillConfig:
    agent_id: str = "raphael"
    limit: int = 1000


@workflow.defn(name="NotesBackfillFlow")
class NotesBackfillFlow:
    @workflow.run
    async def run(self, config: NotesBackfillConfig) -> dict:
        # NO_RETRY: every write is marker-idempotent, so a failed run is simply
        # started again by hand; an automatic retry would only hide the error.
        return await workflow.execute_activity(
            "notes_backfill_journal",
            config.limit,
            start_to_close_timeout=_BACKFILL_TIMEOUT,
            retry_policy=NO_RETRY,
        )
