"""BackupAuditFlow - daily freshness + monthly restore drill."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.homelab import HomelabActivities
    from aegis_worker.shared.retry import (
        FAST,
        NO_RETRY,
        TIMEOUT_FAST,
        TIMEOUT_STANDARD,
    )


_DEFAULT_SETS = ["postgresql", "clickhouse"]
_NFS_BASE_PATH = "/mnt/General/NFS/swarm-backups"


@dataclass
class BackupAuditConfig:
    mode: str = "freshness"  # or "restore_drill"
    backup_sets: list[str] = field(default_factory=lambda: list(_DEFAULT_SETS))
    drill_host: str = "node-b"
    dry_run: bool = False
    silent: bool = False


@workflow.defn
class BackupAuditFlow:
    @workflow.run
    async def run(self, config: BackupAuditConfig) -> dict:
        stale = 0
        drill_ok: bool | None = None
        try:
            if config.mode == "freshness":
                for bset in config.backup_sets:
                    # audit_backup_set returns one summary per discovered
                    # series (e.g. postgresql/postgres_db_miniflux,
                    # postgresql/postgres_db_n8n_db, …) so each database
                    # gets its own freshness + size delta judgment.
                    summaries = await workflow.execute_activity_method(
                        HomelabActivities.audit_backup_set,
                        args=[bset, _NFS_BASE_PATH],
                        start_to_close_timeout=TIMEOUT_STANDARD,
                        retry_policy=FAST,
                    )
                    for summary in summaries:
                        issue = (
                            summary.get("stale")
                            or summary.get("abnormal_size")
                            or summary.get("error")
                        )
                        if issue:
                            stale += 1
                            if not config.silent:
                                # notify_backup_issue uses safe_send_message
                                # internally and never raises — no wrapping
                                # try/except needed.
                                await workflow.execute_activity_method(
                                    HomelabActivities.notify_backup_issue,
                                    args=[summary],
                                    start_to_close_timeout=TIMEOUT_FAST,
                                    retry_policy=NO_RETRY,
                                )
            elif config.mode == "restore_drill":
                # restore_drill mode is manual-trigger only — no scheduled
                # fire. Trigger via POST /api/admin/trigger/backup-audit with
                # mode=restore_drill.
                all_ok = True
                for bset in config.backup_sets:
                    # Inner _docker timeout is 900s; outer activity timeout
                    # must be larger to let a real restore finish. Use 20min.
                    result = await workflow.execute_activity_method(
                        HomelabActivities.run_restore_drill,
                        args=[bset, config.drill_host, config.dry_run],
                        start_to_close_timeout=timedelta(minutes=20),
                        retry_policy=NO_RETRY,
                    )
                    all_ok = all_ok and result["ok"]
                    if not result["ok"] and not config.silent:
                        # notify_backup_issue uses safe_send_message
                        # internally and never raises — no wrapping
                        # try/except needed.
                        await workflow.execute_activity_method(
                            HomelabActivities.notify_backup_issue,
                            args=[
                                {
                                    "backup_set": bset,
                                    "error": f"restore drill failed: {result['notes'][:300]}",
                                }
                            ],
                            start_to_close_timeout=TIMEOUT_FAST,
                            retry_policy=NO_RETRY,
                        )
                drill_ok = all_ok
        except Exception as exc:
            workflow.logger.error("backup_audit_failed error=%s", str(exc)[:200])
            raise
        return {"mode": config.mode, "stale": stale, "drill_ok": drill_ok}
