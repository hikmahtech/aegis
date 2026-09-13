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
    from aegis.services.notes import INDEX_MAX_CHARS

    from aegis_worker.activities.notes import DEFAULT_INDEX_BATCH
    from aegis_worker.shared.retry import RETRY_ONCE

# A batch embeds up to `max_files` notes; a first pass over a large vault is
# the slow case, and it is spread over several runs rather than one long one.
_INDEX_TIMEOUT = timedelta(minutes=30)


@dataclass
class NotesSyncConfig:
    # The activities row's agent; nothing in the flow depends on it.
    agent_id: str = ""
    max_files: int = DEFAULT_INDEX_BATCH
    # One note is indexed whole up to this many characters (the row's
    # `index_max_chars`); a longer one is cut.
    index_max_chars: int = INDEX_MAX_CHARS


@workflow.defn(name="NotesSyncFlow")
class NotesSyncFlow:
    @workflow.run
    async def run(self, config: NotesSyncConfig) -> dict:
        return await workflow.execute_activity(
            "notes_index_vault",
            args=[config.max_files, config.index_max_chars],
            start_to_close_timeout=_INDEX_TIMEOUT,
            retry_policy=RETRY_ONCE,
        )
