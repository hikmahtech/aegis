"""NotesBackfillFlow — the daylog's knowledge rows into the vault journal (#514).

Weekly (`notes-backfill-weekly` in `config/seed/activities.yaml`). The first
run moved the daylog's old rows into the journal. Since then the daylog writes
to the journal itself and files a knowledge row only when the vault write
fails, so a later run finds each such day and puts it where it belongs, and a
week with nothing missing writes nothing. Every write carries the marker the
live daylog uses, so a day already in the journal is left alone.

It can also be started by hand:

    temporal workflow start --type NotesBackfillFlow --task-queue aegis-main \\
      --workflow-id notes-backfill-journal --input '{"agent_id": "raphael"}'
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
    # Daylog rows a run looks at, newest first.
    limit: int = 1000


@workflow.defn(name="NotesBackfillFlow")
class NotesBackfillFlow:
    @workflow.run
    async def run(self, config: NotesBackfillConfig) -> dict:
        # NO_RETRY: every write is marker-idempotent, so a failed run is simply
        # next week's, or started again by hand; an automatic retry would only
        # hide the error.
        return await workflow.execute_activity(
            "notes_backfill_journal",
            config.limit,
            start_to_close_timeout=_BACKFILL_TIMEOUT,
            retry_policy=NO_RETRY,
        )
