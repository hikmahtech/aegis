"""DriveSyncFlow / DriveActivities no-op when no folder is configured."""

from __future__ import annotations

import pytest
from aegis_worker.activities.drive import DriveActivities, SyncDriveFolderInput
from aegis_worker.flows.drive_sync import DriveSyncFlow, DriveSyncInput

pytestmark = pytest.mark.asyncio


async def test_flow_skips_without_folder():
    res = await DriveSyncFlow().run(DriveSyncInput(folder_id=""))
    assert res["status"] == "skipped" and res["reason"] == "no_folder_configured"


async def test_activity_skips_without_folder():
    act = DriveActivities(gmail_token_dir="config/", db_pool=None, knowledge_connector=object())
    res = await act.sync_drive_folder(SyncDriveFolderInput(account="x", folder_id=""))
    assert res["status"] == "skipped"
    assert res["ingested"] == 0 and res["unchanged"] == 0
