"""Drive-watch activity — incrementally ingest a tracked Google Drive folder."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from temporalio import activity


@dataclass
class SyncDriveFolderInput:
    account: str
    folder_id: str
    recurse: bool = True
    source_type: str = "drive"


@dataclass
class DriveActivities:
    gmail_token_dir: str
    db_pool: Any
    knowledge_connector: Any = None

    @activity.defn
    async def sync_drive_folder(self, input: SyncDriveFolderInput) -> dict:
        """Ingest new/changed files from the tracked folder. Skips unchanged docs
        (by Drive modifiedTime), so re-runs only embed what actually changed."""
        if not input.folder_id or not self.knowledge_connector:
            return {
                "status": "skipped",
                "reason": "no_folder_configured" if not input.folder_id else "no_store",
                "ingested": 0,
                "unchanged": 0,
                "skipped": 0,
                "errors": 0,
            }
        from aegis.services.drive import ingest_drive_folder

        token_path = Path(self.gmail_token_dir) / f"{input.account}.json"
        rows = await self.db_pool.fetch(
            "SELECT content_id, metadata->>'drive_modified_time' AS mt "
            "FROM knowledge_content WHERE source_type = $1",
            input.source_type,
        )
        skip = {r["content_id"]: r["mt"] for r in rows if r["mt"]}

        result = await ingest_drive_folder(
            self.knowledge_connector,
            token_path,
            input.folder_id,
            source_type=input.source_type,
            recurse=input.recurse,
            skip_unchanged=skip,
        )
        is_empty = not any(
            result[k] for k in ("ingested", "unchanged", "skipped", "errors")
        )
        if is_empty:
            # Issue #111: 122/122 DriveSyncFlow runs silently reported "ok" with
            # every counter at zero (the folder listing came back empty), and
            # nothing ever surfaced it. A folder listing 0 files total is
            # distinguishable from "already synced" (which shows up as
            # unchanged>0) so flag it loudly instead of letting it masquerade
            # as a normal run.
            activity.logger.warning(
                "drive_folder_empty_listing account=%s folder_id=%s — listing "
                "returned 0 files (ingested/unchanged/skipped/errors all zero)",
                input.account,
                input.folder_id,
            )
        else:
            activity.logger.info(
                "drive_folder_synced folder=%s ingested=%s unchanged=%s skipped=%s errors=%s",
                input.folder_id,
                result["ingested"],
                result["unchanged"],
                result["skipped"],
                result["errors"],
            )
        return {"status": "empty_listing" if is_empty else "ok", **result}
