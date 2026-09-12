"""NotesBackfillFlow — the daylog's knowledge rows into the vault journal (#514).

Weekly (`notes-backfill-weekly` in `config/seed/activities.yaml`). The first
run moved the daylog's old rows into the journal. Since then the daylog writes
to the journal itself and files a knowledge row only when the vault write
fails, so a later run finds each such day and puts it where it belongs, and a
week with nothing missing writes nothing. Every write carries the marker the
live daylog uses, so a day already in the journal is left alone.

The scheduled run looks only at rows filed in the last `since_days` days (14
in the seed: two weekly chances at each fallback day). The pre-vault rows are
still in the store, so rereading all of them every week would put back a
block the user had deleted from an old journal note on the phone.

It can also be started by hand, with `since_days` 0 (every row):

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
    # Only rows filed in the last this-many days; 0 = every row. The seed row
    # sets 14; a run started by hand takes every row.
    since_days: int = 0


@workflow.defn(name="NotesBackfillFlow")
class NotesBackfillFlow:
    @workflow.run
    async def run(self, config: NotesBackfillConfig) -> dict:
        # NO_RETRY: every write is marker-idempotent, so a failed run is simply
        # next week's, or started again by hand; an automatic retry would only
        # hide the error.
        return await workflow.execute_activity(
            "notes_backfill_journal",
            args=[config.limit, config.since_days],
            start_to_close_timeout=_BACKFILL_TIMEOUT,
            retry_policy=NO_RETRY,
        )
