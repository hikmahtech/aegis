"""DriveSyncFlow / DriveActivities no-op when no folder is configured."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

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


# ── empty-listing guard (issue #111) ────────────────────────────────
#
# 122/122 real DriveSyncFlow runs reported status="ok" with every counter at
# zero (the Drive folder listing itself came back empty) and nothing ever
# surfaced it. These tests pin the guard: an all-zero folder listing must be
# distinguishable ("empty_listing") from a real "ok" run, and must log a
# WARNING with the account/folder identifying it — without touching the
# folder/auth fix itself, which is being handled ops-side.


class _FakePool:
    async def fetch(self, *_a, **_kw):
        return []


async def test_activity_flags_empty_listing_as_distinct_status(caplog):
    """All four counters at zero -> status='empty_listing' (not 'ok'), and a
    WARNING naming the account/folder_id is logged."""
    act = DriveActivities(
        gmail_token_dir="config/", db_pool=_FakePool(), knowledge_connector=object()
    )
    empty_result = {"ingested": 0, "unchanged": 0, "skipped": 0, "errors": 0}
    with (
        patch(
            "aegis.services.drive.ingest_drive_folder",
            new=AsyncMock(return_value=empty_result),
        ),
        caplog.at_level("WARNING"),
    ):
        res = await act.sync_drive_folder(
            SyncDriveFolderInput(account="arshad-personal", folder_id="folder-123")
        )

    assert res["status"] == "empty_listing"
    assert res["ingested"] == 0 and res["errors"] == 0
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        "empty_listing" in r.message or "empty" in r.message.lower() for r in warnings
    ), f"expected an empty-listing warning, got: {[r.message for r in caplog.records]}"
    assert any("arshad-personal" in r.message for r in warnings)
    assert any("folder-123" in r.message for r in warnings)


async def test_activity_reports_ok_when_listing_has_files():
    """A non-empty listing (e.g. files unchanged from a prior sync) keeps
    status='ok' — the guard must not misfire on a healthy incremental sync."""
    act = DriveActivities(
        gmail_token_dir="config/", db_pool=_FakePool(), knowledge_connector=object()
    )
    nonempty_result = {"ingested": 0, "unchanged": 5, "skipped": 0, "errors": 0}
    with patch(
        "aegis.services.drive.ingest_drive_folder",
        new=AsyncMock(return_value=nonempty_result),
    ):
        res = await act.sync_drive_folder(
            SyncDriveFolderInput(account="arshad-personal", folder_id="folder-123")
        )
    assert res["status"] == "ok"
    assert res["unchanged"] == 5


async def test_flow_surfaces_empty_listing_status_in_aggregate():
    """The flow's aggregate result_summary status must flip to 'empty_listing'
    when a tracked folder's activity reports one, not stay 'ok'."""
    with patch(
        "temporalio.workflow.execute_activity",
        new=AsyncMock(
            return_value={
                "status": "empty_listing",
                "ingested": 0,
                "unchanged": 0,
                "skipped": 0,
                "errors": 0,
            }
        ),
    ):
        res = await DriveSyncFlow().run(
            DriveSyncInput(account="arshad-personal", folder_id="folder-123")
        )

    assert res["status"] == "empty_listing"
    assert res["empty_folder_ids"] == ["folder-123"]
    assert res["ingested"] == 0 and res["errors"] == 0


async def test_flow_keeps_ok_status_when_no_folder_reports_empty():
    with patch(
        "temporalio.workflow.execute_activity",
        new=AsyncMock(
            return_value={
                "status": "ok",
                "ingested": 2,
                "unchanged": 1,
                "skipped": 0,
                "errors": 0,
            }
        ),
    ):
        res = await DriveSyncFlow().run(
            DriveSyncInput(account="arshad-personal", folder_id="folder-123")
        )

    assert res["status"] == "ok"
    assert "empty_folder_ids" not in res
