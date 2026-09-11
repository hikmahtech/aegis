"""CalibreSyncFlow — daily refresh of the knowledge store's book index (#510).

One step: `sync_calibre_library` makes the `source_type='book'` rows match the
Calibre library (metadata only — a book's text is read on demand by the
library tools, never indexed). Inert until the Integrations page has a
calibre-web user and password: the run then reports `not_configured`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.shared.retry import ACT_RETRY

# 235 books, each re-embedded only when it changed: minutes at most, even on
# the first run when every book is new.
_SYNC_TIMEOUT = timedelta(minutes=15)


@dataclass
class CalibreSyncConfig:
    agent_id: str = "raphael"


@workflow.defn(name="CalibreSyncFlow")
class CalibreSyncFlow:
    @workflow.run
    async def run(self, config: CalibreSyncConfig) -> dict:
        return await workflow.execute_activity(
            "sync_calibre_library",
            start_to_close_timeout=_SYNC_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
