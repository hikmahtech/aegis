"""NotesSyncFlow — keep the knowledge index in step with the vault (#514).

Hourly. Pull the vault, index what changed since the last pass as
`source_type='note'` (encrypted blocks stripped first), drop deleted notes from
the index. The vault is the record; this row set is only its index, the same
split as the books and `finance.journal_index`.

Inert — `status: not_configured` — until the Integrations page has both
`notes_repo_url` and `notes_deploy_key`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.notes import DEFAULT_INDEX_BATCH
    from aegis_worker.shared.retry import RETRY_ONCE

# A batch embeds up to `max_files` notes; a first pass over a large vault is
# the slow case, and it is spread over several runs rather than one long one.
_INDEX_TIMEOUT = timedelta(minutes=30)


@dataclass
class NotesSyncConfig:
    agent_id: str = "raphael"
    max_files: int = DEFAULT_INDEX_BATCH


@workflow.defn(name="NotesSyncFlow")
class NotesSyncFlow:
    @workflow.run
    async def run(self, config: NotesSyncConfig) -> dict:
        return await workflow.execute_activity(
            "notes_index_vault",
            config.max_files,
            start_to_close_timeout=_INDEX_TIMEOUT,
            retry_policy=RETRY_ONCE,
        )
