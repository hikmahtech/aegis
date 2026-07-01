"""DriveSyncFlow — scheduled incremental ingest of a tracked Google Drive folder.

Reads {account, folder_id, recurse} from the activity's config (schedule_sync
mapper). No-ops when no folder is configured, so the schedule can exist before
the owner designates a folder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.drive import SyncDriveFolderInput
    from aegis_worker.shared.retry import RETRY_ONCE

_SYNC_TIMEOUT = timedelta(minutes=15)


@dataclass
class DriveSyncInput:
    agent_id: str = "raphael"
    account: str = ""
    folder_id: str = ""  # legacy single-folder config
    folders: list[dict] = field(default_factory=list)  # [{id, name}] — preferred
    recurse: bool = True
    source_type: str = "drive"


@workflow.defn(name="DriveSyncFlow")
class DriveSyncFlow:
    @workflow.run
    async def run(self, input: DriveSyncInput) -> dict:
        folders = input.folders or ([{"id": input.folder_id}] if input.folder_id else [])
        folders = [f for f in folders if f.get("id")]
        if not folders:
            return {"status": "skipped", "reason": "no_folder_configured"}
        agg = {"ingested": 0, "unchanged": 0, "skipped": 0, "errors": 0, "folders": 0}
        for f in folders:
            r = await workflow.execute_activity(
                "sync_drive_folder",
                SyncDriveFolderInput(
                    account=input.account,
                    folder_id=f["id"],
                    recurse=input.recurse,
                    source_type=input.source_type,
                ),
                start_to_close_timeout=_SYNC_TIMEOUT,
                retry_policy=RETRY_ONCE,
            )
            for k in ("ingested", "unchanged", "skipped", "errors"):
                agg[k] += r.get(k, 0)
            agg["folders"] += 1
        return {"status": "ok", **agg}
