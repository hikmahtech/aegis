"""Activity-to-schedule synchronization.

Reads active activities from the database and registers them as
Temporal schedules. Idempotent — safe to call on every worker startup.
"""

from __future__ import annotations

import asyncpg
import structlog
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleSpec,
    ScheduleUpdate,
    ScheduleUpdateInput,
)

from aegis_worker.flows.calendar_ingest import CalendarIngestFlow, CalendarIngestInput
from aegis_worker.flows.cert_radar import CertRadarConfig, CertRadarFlow
from aegis_worker.flows.clarify import ClarifyConfig, ClarifyFlow
from aegis_worker.flows.cleanup import CleanupConfig, CleanupFlow
from aegis_worker.flows.daily_briefing import DailyBriefingConfig, DailyBriefingFlow
from aegis_worker.flows.delivery_watchdog import DeliveryWatchdogConfig, DeliveryWatchdogFlow
from aegis_worker.flows.drive_sync import DriveSyncFlow, DriveSyncInput
from aegis_worker.flows.gmail_ingest import GmailIngestFlow, GmailIngestInput
from aegis_worker.flows.intelligence_scan import IntelligenceScanFlow, IntelligenceScanInput
from aegis_worker.flows.memory_reflection import MemoryReflectionFlow, MemoryReflectionInput
from aegis_worker.flows.money_hygiene import MoneyHygieneConfig, MoneyHygieneDailyFlow
from aegis_worker.flows.raindrop_ingest import RaindropIngestFlow, RaindropIngestInput
from aegis_worker.flows.receipt_ingest import ReceiptIngestFlow, ReceiptIngestInput
from aegis_worker.flows.review import (
    DailyReviewConfig,
    DailyReviewFlow,
    WeeklyReviewConfig,
    WeeklyReviewFlow,
)
from aegis_worker.flows.rss_ingest import RssIngestFlow, RssIngestInput
from aegis_worker.flows.sentry_poll import SentryPollFlow, SentryPollInput
from aegis_worker.flows.service_drift import ServiceDriftConfig, ServiceDriftFlow
from aegis_worker.flows.social_metrics import SocialMetricsConfig, SocialMetricsFlow
from aegis_worker.flows.social_publish import SocialPublishConfig, SocialPublishFlow
from aegis_worker.flows.subscription_audit import SubscriptionAuditConfig, SubscriptionAuditFlow
from aegis_worker.flows.todoist_sync import TodoistSyncConfig, TodoistSyncFlow
from aegis_worker.flows.vercel_project_sync import VercelProjectSyncFlow, VercelProjectSyncInput
from aegis_worker.flows.workspace_repo_sync import (
    WorkspaceRepoSyncFlow,
    WorkspaceRepoSyncInput,
)

logger = structlog.get_logger()

# Map activity type → (workflow class, config builder)
_ACTIVITY_TYPE_MAP = {
    "DailyBriefingFlow": lambda act: (
        DailyBriefingFlow,
        DailyBriefingConfig(
            agent_id=act["agent_id"],
        ),
    ),
    "CleanupFlow": lambda act: (
        CleanupFlow,
        CleanupConfig(
            retentions=act["config"].get("retentions") or {},
        ),
    ),
    "ServiceDriftFlow": lambda act: (
        ServiceDriftFlow,
        ServiceDriftConfig(
            silent=bool(act["config"].get("silent", False)),
            recheck_delay_seconds=int(act["config"].get("recheck_delay_seconds", 120)),
        ),
    ),
    "DeliveryWatchdogFlow": lambda act: (
        DeliveryWatchdogFlow,
        DeliveryWatchdogConfig(
            silent=bool(act["config"].get("silent", False)),
            threshold_seconds=int(act["config"].get("threshold_seconds", 120)),
            window_hours=int(act["config"].get("window_hours", 24)),
            comms_url=act["_settings"].get("comms_url", ""),
        ),
    ),
    "CertRadarFlow": lambda act: (
        CertRadarFlow,
        CertRadarConfig(
            silent=bool(act["config"].get("silent", False)),
            domains=act["config"].get("domains", []),
        ),
    ),
    # v3 Phase 3 — ingest flows + learning loop.
    # workflow_type in the seed is the workflow class name (PascalCase).
    # Config fields come from settings; the seed only carries tuning knobs.
    # GmailIngestFlow/ReceiptIngestFlow receive aegis_ui_url via the
    # settings-aware builder (see act["_settings"] injection below).
    "GmailIngestFlow": lambda act: (
        GmailIngestFlow,
        GmailIngestInput(
            agent_id=act["agent_id"],
            max_per_account=int(act["config"].get("max_per_account", 50)),
            query=act["config"].get("query", "is:unread newer_than:2d"),
            aegis_ui_url=act["_settings"].get("aegis_ui_url", ""),
        ),
    ),
    "CalendarIngestFlow": lambda act: (
        CalendarIngestFlow,
        CalendarIngestInput(
            agent_id=act["agent_id"],
            horizon_days=int(act["config"].get("horizon_days", 30)),
        ),
    ),
    "ReceiptIngestFlow": lambda act: (
        ReceiptIngestFlow,
        ReceiptIngestInput(
            agent_id=act["agent_id"],
            max_per_account=int(act["config"].get("max_per_account", 50)),
            query_window=act["config"].get("query_window", "newer_than:14d"),
            aegis_ui_url=act["_settings"].get("aegis_ui_url", ""),
        ),
    ),
    "RaindropIngestFlow": lambda act: (
        RaindropIngestFlow,
        RaindropIngestInput(agent_id=act["agent_id"]),
    ),
    "RssIngestFlow": lambda act: (
        RssIngestFlow,
        RssIngestInput(agent_id=act["agent_id"]),
    ),
    "DriveSyncFlow": lambda act: (
        DriveSyncFlow,
        DriveSyncInput(
            agent_id=act["agent_id"],
            account=(act["config"] or {}).get("account", ""),
            folder_id=(act["config"] or {}).get("folder_id", ""),
            folders=(act["config"] or {}).get("folders") or [],
            recurse=(act["config"] or {}).get("recurse", True),
            source_type=(act["config"] or {}).get("source_type", "drive"),
        ),
    ),
    "MemoryReflectionFlow": lambda act: (
        MemoryReflectionFlow,
        MemoryReflectionInput(
            agent_id=act["agent_id"],
            keep=int((act["config"] or {}).get("keep", 50)),
        ),
    ),
    "IntelligenceScanFlow": lambda act: (
        IntelligenceScanFlow,
        IntelligenceScanInput(
            agent_id=act["agent_id"],
            source=act["config"].get("source", "hn"),
            topics=list(act["config"].get("topics") or []),
            max_results=int(act["config"].get("max_results", 20)),
            significance_threshold=int(act["config"].get("significance_threshold", 4)),
        ),
    ),
    "SentryPollFlow": lambda act: (
        SentryPollFlow,
        SentryPollInput(
            agent_id=act["agent_id"],
            mode=act["config"].get("mode", "poll"),
            limit=int(act["config"].get("limit", 25)),
        ),
    ),
    "MoneyHygieneDailyFlow": lambda act: (
        MoneyHygieneDailyFlow,
        MoneyHygieneConfig(
            agent_id=act["agent_id"],
            silent=bool(act["config"].get("silent", False)),
            threshold_multiplier=float(act["config"].get("threshold_multiplier", 2.0)),
            thresholds_days=act["config"].get("thresholds_days", [30, 14, 7, 0]),
        ),
    ),
    "SubscriptionAuditFlow": lambda act: (
        SubscriptionAuditFlow,
        SubscriptionAuditConfig(
            agent_id=act["agent_id"],
            silent=bool(act["config"].get("silent", False)),
        ),
    ),
    "TodoistSyncFlow": lambda act: (
        TodoistSyncFlow,
        TodoistSyncConfig(
            agent_id=act["agent_id"],
        ),
    ),
    "ClarifyFlow": lambda act: (
        ClarifyFlow,
        ClarifyConfig(
            agent_id=act["agent_id"],
            max_items=int((act.get("config") or {}).get("max_items") or 20),
        ),
    ),
    "DailyReviewFlow": lambda act: (
        DailyReviewFlow,
        DailyReviewConfig(
            agent_id=act["agent_id"],
        ),
    ),
    "WeeklyReviewFlow": lambda act: (
        WeeklyReviewFlow,
        WeeklyReviewConfig(
            agent_id=act["agent_id"],
        ),
    ),
    "SocialPublishFlow": lambda act: (
        SocialPublishFlow,
        SocialPublishConfig(
            agent_id=act["agent_id"],
            lookahead_minutes=int(act["config"].get("lookahead_minutes", 10)),
            default_post_hour=int(act["config"].get("default_post_hour", 9)),
        ),
    ),
    "SocialMetricsFlow": lambda act: (
        SocialMetricsFlow,
        SocialMetricsConfig(
            agent_id=act["agent_id"],
            window_days=int(act["config"].get("window_days", 14)),
        ),
    ),
    "WorkspaceRepoSyncFlow": lambda act: (
        WorkspaceRepoSyncFlow,
        WorkspaceRepoSyncInput(
            agent_id=act["agent_id"],
            min_repos=int(act["config"].get("min_repos", 5)),
        ),
    ),
    "VercelProjectSyncFlow": lambda act: (
        VercelProjectSyncFlow,
        VercelProjectSyncInput(
            agent_id=act["agent_id"],
            include_personal=bool(act["config"].get("include_personal", True)),
            team_ids=list(act["config"].get("team_ids") or []),
        ),
    ),
}

# Activity types whose owning flow is only registered on the worker behind a
# feature flag (see worker/__main__.py). Their seed rows ship active=true, so
# without this gate schedule_sync would create Temporal schedules that fire
# against a workflow type the worker never registered. Keyed by the settings
# flag → the types it guards. When the flag is off we skip the row (and, since
# it never enters expected_ids, the prune pass deletes any stale schedule —
# so toggling a flag off cleans up too).
_FEATURE_FLAGGED_TYPES = {
    "homelab_enabled": {"ServiceDriftFlow", "DeliveryWatchdogFlow", "CertRadarFlow"},
    "money_hygiene_enabled": {
        "ReceiptIngestFlow",
        "MoneyHygieneDailyFlow",
        "SubscriptionAuditFlow",
    },
}


def _disabled_by_feature_flag(act_type: str, settings: object | None) -> str | None:
    """Return the settings flag name gating `act_type` if it's off, else None.

    settings=None (e.g. some tests) means "don't gate" — behaves as before.
    """
    if settings is None:
        return None
    for flag, types in _FEATURE_FLAGGED_TYPES.items():
        if act_type in types and not getattr(settings, flag, False):
            return flag
    return None


async def sync_schedules(
    client: Client,
    pool: asyncpg.Pool,
    task_queue: str = "aegis-main",
    settings: object | None = None,
) -> int:
    """Sync Temporal schedules from active activities in the database.

    Returns the number of schedules registered.

    When settings is provided, type-specific defaults are injected into the
    activity config — e.g. cert_radar domains fall back to
    settings.homelab_public_domains so a freshly-seeded activity row with
    an empty config still probes the right domains.
    """
    import json

    # Fetch active activities with cron schedules (v3 schema)
    rows = await pool.fetch(
        "SELECT id, slug, workflow_type, agent_id, schedule_cron, config "
        "FROM activities WHERE active = TRUE AND schedule_cron IS NOT NULL"
    )

    registered = 0
    expected_ids = set()

    for row in rows:
        act = dict(row)
        act_name = act["slug"]
        act_type = act["workflow_type"]
        cron = act["schedule_cron"]

        # Skip flows whose owning feature flag is off — the worker didn't
        # register the workflow type, so a schedule for it would only error.
        gated_off = _disabled_by_feature_flag(act_type, settings)
        if gated_off:
            logger.info(
                "schedule_skipped_feature_off",
                activity=act_name,
                type=act_type,
                flag=gated_off,
            )
            continue

        # Parse config
        config = act.get("config")
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except (json.JSONDecodeError, TypeError):
                config = {}
        act["config"] = config or {}

        # Settings-backed defaults for homelab flows
        if (
            settings is not None
            and act_type == "CertRadarFlow"
            and not act["config"].get("domains")
        ):
            fallback = getattr(settings, "homelab_public_domains", None) or []
            if fallback:
                act["config"]["domains"] = list(fallback)

        # Expose selected settings fields to mappers (Gmail/Receipt reauth link,
        # comms service URL). Mappers read via act["_settings"].get(...).
        act["_settings"] = {
            "aegis_ui_url": getattr(settings, "aegis_ui_url", "") if settings else "",
            "comms_url": (
                getattr(settings, "comms_url", "") if settings else ""
            ),
        }

        # Map to workflow + config
        mapper = _ACTIVITY_TYPE_MAP.get(act_type)
        if not mapper:
            logger.warning("schedule_unknown_type", activity=act_name, type=act_type)
            continue

        workflow_cls, flow_config = mapper(act)
        schedule_id = act_name
        expected_ids.add(schedule_id)

        schedule = Schedule(
            action=ScheduleActionStartWorkflow(
                workflow_cls.run,
                args=[flow_config],
                task_queue=task_queue,
                id=f"scheduled-{schedule_id}",
            ),
            spec=ScheduleSpec(cron_expressions=[cron]),
        )

        try:
            # Try to update existing schedule
            handle = client.get_schedule_handle(schedule_id)
            await handle.describe()

            async def _updater(_input: ScheduleUpdateInput, s=schedule) -> ScheduleUpdate:
                return ScheduleUpdate(schedule=s)

            await handle.update(_updater)
            logger.info("schedule_updated", schedule_id=schedule_id)
        except Exception:
            # Create new schedule
            try:
                await client.create_schedule(schedule_id, schedule)
                logger.info("schedule_created", schedule_id=schedule_id, cron=cron)
            except Exception as e:
                logger.warning("schedule_create_failed", schedule_id=schedule_id, error=str(e))
                continue

        registered += 1

    # Prune orphaned schedules
    try:
        async for sched in await client.list_schedules():
            if sched.id not in expected_ids:
                try:
                    handle = client.get_schedule_handle(sched.id)
                    await handle.delete()
                    logger.info("schedule_deleted_orphan", schedule_id=sched.id)
                except Exception as exc:
                    logger.warning(
                        "schedule_delete_orphan_failed",
                        schedule_id=sched.id,
                        error=str(exc),
                    )
    except Exception as exc:
        logger.warning("schedule_prune_failed", error=str(exc))

    logger.info("schedule_sync_complete", registered=registered, total_activities=len(rows))
    return registered
