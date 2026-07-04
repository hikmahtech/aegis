"""AEGIS v2 Worker entrypoint.

Connects to Temporal, bootstraps dependencies, registers all flows
and activities, syncs schedules from the activities table, then runs.
"""

from __future__ import annotations

import asyncio
import os

import structlog
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.worker import Worker

from aegis_worker.activities.active_work import ActiveWorkActivities
from aegis_worker.activities.alert_governance import AlertGovernanceActivities
from aegis_worker.activities.alerts import AlertActivities
from aegis_worker.activities.briefing import BriefingActivities
from aegis_worker.activities.calendar import CalendarActivities
from aegis_worker.activities.capture import CaptureActivities
from aegis_worker.activities.channels import ChannelActivities
from aegis_worker.activities.chat import ChatActivities
from aegis_worker.activities.clarify import ClarifyActivities
from aegis_worker.activities.cleanup import CleanupActivities
from aegis_worker.activities.content import ContentActivities
from aegis_worker.activities.core_client import CoreClient
from aegis_worker.activities.delivery import DeliveryActivities
from aegis_worker.activities.drive import DriveActivities
from aegis_worker.activities.gmail import GmailActivities
from aegis_worker.activities.homelab import HomelabActivities
from aegis_worker.activities.intel_scan import IntelScanActivities
from aegis_worker.activities.intelligence import IntelligenceActivities
from aegis_worker.activities.interactions import InteractionActivities
from aegis_worker.activities.inventory import InventoryActivities
from aegis_worker.activities.memory import MemoryActivities
from aegis_worker.activities.money import MoneyActivities
from aegis_worker.activities.raindrop import RaindropActivities
from aegis_worker.activities.review import ReviewActivities
from aegis_worker.activities.rss import RssActivities
from aegis_worker.activities.runs_v3 import RunRecorderActivities
from aegis_worker.activities.sentry_ingest import SentryIngestActivities
from aegis_worker.activities.social import SocialActivities
from aegis_worker.activities.todoist import TodoistActivities
from aegis_worker.bootstrap import bootstrap
from aegis_worker.flows.agent_chat_reply import AgentChatReplyFlow
from aegis_worker.flows.alert_investigation import AlertInvestigationFlow
from aegis_worker.flows.calendar_ingest import CalendarIngestFlow
from aegis_worker.flows.cert_radar import CertRadarFlow
from aegis_worker.flows.clarify import ClarifyFlow
from aegis_worker.flows.cleanup import CleanupFlow
from aegis_worker.flows.daily_briefing import DailyBriefingFlow
from aegis_worker.flows.delivery_watchdog import DeliveryWatchdogFlow
from aegis_worker.flows.drive_sync import DriveSyncFlow
from aegis_worker.flows.github_alert import GitHubAlertFlow
from aegis_worker.flows.gmail_ingest import GmailIngestFlow
from aegis_worker.flows.intelligence_scan import IntelligenceScanFlow
from aegis_worker.flows.interaction import InteractionFlow
from aegis_worker.flows.memory_reflection import MemoryReflectionFlow
from aegis_worker.flows.money_hygiene import MoneyHygieneDailyFlow
from aegis_worker.flows.money_process import MoneyProcessFlow
from aegis_worker.flows.raindrop_ingest import RaindropIngestFlow
from aegis_worker.flows.receipt_ingest import ReceiptIngestFlow
from aegis_worker.flows.review import DailyReviewFlow, WeeklyReviewFlow
from aegis_worker.flows.rss_ingest import RssIngestFlow
from aegis_worker.flows.sentry_poll import SentryPollFlow
from aegis_worker.flows.service_drift import ServiceDriftFlow
from aegis_worker.flows.social_metrics import SocialMetricsFlow
from aegis_worker.flows.social_publish import SocialPublishFlow
from aegis_worker.flows.subscription_audit import SubscriptionAuditFlow
from aegis_worker.flows.todoist_sync import TodoistSyncFlow
from aegis_worker.flows.vercel_project_sync import VercelProjectSyncFlow
from aegis_worker.flows.workspace_repo_sync import WorkspaceRepoSyncFlow
from aegis_worker.interceptors import WorkflowRunRecorderInterceptor
from aegis_worker.schedule_sync import sync_schedules

logger = structlog.get_logger()

TASK_QUEUE = "aegis-main"

# ---------------------------------------------------------------------------
# Module-level registration lists
#
# WORKFLOWS: pure list of workflow classes.  Stable at import time — no
#   runtime dependencies needed.
#
# ACTIVITIES: list of bound activity methods.  Built from stub instances
#   (all deps=None) at module level so registration tests can inspect them
#   without connecting to Temporal, Postgres, or any external service.
#   main() replaces these with the real instances that have live deps.
# ---------------------------------------------------------------------------

# Stub instances for module-level ACTIVITIES (deps=None is safe at import
# time; the dataclass @activity.defn decorator binds the name to the
# method object, not to the dependency values).
_stub_chat_act = ChatActivities(client=None)  # type: ignore[arg-type]
_stub_clarify_act = ClarifyActivities(db_pool=None)
_stub_social_act = SocialActivities(db_pool=None)

WORKFLOWS: list = [
    AgentChatReplyFlow,
    AlertInvestigationFlow,
    CalendarIngestFlow,
    DailyBriefingFlow,
    CleanupFlow,
    InteractionFlow,
    GmailIngestFlow,
    GitHubAlertFlow,
    RaindropIngestFlow,
    RssIngestFlow,
    DriveSyncFlow,
    MemoryReflectionFlow,
    IntelligenceScanFlow,
    SentryPollFlow,
    TodoistSyncFlow,
    ClarifyFlow,
    DailyReviewFlow,
    WeeklyReviewFlow,
    SocialPublishFlow,
    SocialMetricsFlow,
]

ACTIVITIES: list = [
    _stub_chat_act.synthesize_reply,
    _stub_clarify_act.post_agent_reply_comment,
    _stub_clarify_act.post_agent_reply_error_comment,
    _stub_clarify_act.clear_clarify_watermark,
    _stub_social_act.find_due_posts,
    _stub_social_act.enqueue_outbox,
    _stub_social_act.drain_social_outbox,
    _stub_social_act.complete_posted_tasks,
    _stub_social_act.unpublish_task,
    _stub_social_act.apply_social_approval,
    _stub_social_act.refresh_post_metrics,
]


async def run_periodic_schedule_sync(
    client,
    pool,
    task_queue: str,
    settings,
    interval_seconds: float = 300.0,
    sync_fn=None,
) -> None:
    """Periodic re-sync of Temporal schedules from the activities table.

    Decouples schedule registration from worker boot order: even if
    migrations finish AFTER the worker first boots (the race documented
    in cmemory lesson 096fe6e2), the next periodic pass picks up the
    new rows.

    `sync_fn` is injectable for tests; defaults to `sync_schedules`.
    """
    fn = sync_fn or sync_schedules
    while True:
        try:
            await fn(client, pool, task_queue, settings=settings)
        except Exception as exc:
            logger.warning("periodic_schedule_sync_failed", error=str(exc))
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise


async def main():
    """Bootstrap dependencies, register flows, sync schedules, run worker."""
    # Telemetry first so subsequent init is captured under the service resource.
    # Imported from aegis-core (a declared worker dependency); service.name stays
    # aegis-worker via the OTEL_SERVICE_NAME env var.
    from aegis.telemetry import setup_telemetry

    setup_telemetry()

    # Bootstrap
    deps = await bootstrap()
    settings = deps.settings
    # Model names from the configurable backend (Phase A), env settings as fallback.
    model_balanced = deps.model_tiers.get("balanced") or settings.model_balanced
    model_fast = deps.model_tiers.get("fast") or settings.model_fast

    # Connect to Temporal
    temporal_host = getattr(settings, "temporal_host", "localhost:7233")
    client = await Client.connect(temporal_host)
    logger.info("temporal_connected", host=temporal_host)

    # Create activity instances with real dependencies + connectors
    connectors = deps.connectors

    active_work_act = ActiveWorkActivities(
        db_pool=deps.pool,
        remote_script=connectors.get("remote_script"),
        lookback_hours=settings.active_work_lookback_hours,
    )
    alert_governance_act = AlertGovernanceActivities(
        db_pool=deps.pool,
        remote_script=connectors.get("remote_script"),
    )

    alert_act = AlertActivities(
        db_pool=deps.pool,
        llm_client=deps.llm,
        knowledge_connector=connectors.get("knowledge"),
        remote_script=connectors.get("remote_script"),
        model_balanced=model_balanced,
        kimi_binary=getattr(settings, "kimi_cli_binary_path", "") or "",
        claude_personal_config_dir=getattr(settings, "claude_personal_config_dir", "") or "",
        runbooks_dir=getattr(settings, "runbooks_dir", "/app/runbooks") or "",
        homelab_connector=connectors.get("homelab"),
        temporal_ui_url=getattr(settings, "temporal_ui_url", "") or "",
        # temporal_namespace defaults to "default" on the dataclass — the worker
        # client connects to the "default" namespace too (see Client.connect
        # below). Wire a settings field here if a non-default namespace is added.
    )
    briefing_act = BriefingActivities(
        db_pool=deps.pool,
        llm_client=deps.llm,
        knowledge_connector=connectors.get("knowledge"),
        core_api_url=getattr(settings, "core_api_url", "http://localhost:8080"),
        api_key=getattr(settings, "api_key", ""),
        frame_model=getattr(settings, "model_balanced", "gpt-oss:20b"),
    )
    # Effective channel: an explicit AEGIS_CHANNEL wins; otherwise infer from
    # whether comms is wired (prod sets the comms URL but not AEGIS_CHANNEL on
    # core/worker → slack; a forker with no comms URL → web). Keeps prod's Slack
    # delivery working without an infra change.
    effective_channel = (
        settings.channel
        if os.environ.get("AEGIS_CHANNEL")
        else ("slack" if settings.comms_url else "web")
    )
    delivery_act = DeliveryActivities(
        comms_url=settings.comms_url,
        api_key=settings.api_key,
        tts_enabled=getattr(settings, "tts_enabled", False),
        db_pool=deps.pool,
        budget_enabled=getattr(settings, "notification_budget_enabled", False),
        daily_budget=getattr(settings, "notification_daily_budget", 8),
        channel=effective_channel,
    )
    content_act = ContentActivities(
        knowledge_connector=connectors.get("knowledge"),
        db_pool=deps.pool,
        enabled=getattr(settings, "content_extraction_enabled", True),
        elevenlabs_api_key=getattr(settings, "elevenlabs_api_key", ""),
        elevenlabs_stt_model=getattr(settings, "elevenlabs_stt_model", "scribe_v1"),
        raindrop_api_token=getattr(settings, "raindrop_api_token", ""),
    )
    intel_act = IntelligenceActivities(
        knowledge_connector=connectors.get("knowledge"),
        llm_client=deps.llm,
        model_light=model_fast,
        db_pool=deps.pool,
    )
    cleanup_act = CleanupActivities(
        db_pool=deps.pool,
        comms_url=settings.comms_url,
        api_key=settings.api_key,
    )
    interaction_act = InteractionActivities(db_pool=deps.pool)
    run_recorder_act = RunRecorderActivities(db_pool=deps.pool)

    homelab_act = None
    if settings.homelab_enabled:
        homelab_act = HomelabActivities(
            db_pool=deps.pool,
            homelab=connectors.get("homelab"),
            delivery=delivery_act,
        )

    money_act = None
    if settings.money_hygiene_enabled:
        money_act = MoneyActivities(
            db_pool=deps.pool,
            llm=deps.llm,
            delivery=delivery_act,
            fx_rates=getattr(settings, "money_hygiene_inr_fallback_rates", {}),
        )

    channel_act = ChannelActivities(db_pool=deps.pool)
    calendar_act = CalendarActivities(
        gmail_credentials_file=getattr(
            settings, "gmail_credentials_file", "config/google_credentials.json"
        ),
        gmail_token_dir=getattr(settings, "gmail_token_dir", "config/"),
        aegis_ui_url=getattr(settings, "aegis_ui_url", ""),
    )
    gmail_act = GmailActivities(
        gmail_credentials_file=getattr(
            settings, "gmail_credentials_file", "config/google_credentials.json"
        ),
        gmail_token_dir=getattr(settings, "gmail_token_dir", "config/"),
        aegis_ui_url=getattr(settings, "aegis_ui_url", ""),
        llm_client=deps.llm,
        db_pool=deps.pool,
        knowledge_connector=connectors.get("knowledge"),
        # Without this, GmailActivities keeps its dataclass default
        # model_balanced="qwen3:14b" and ignores AEGIS_MODEL_BALANCED — email
        # triage was running entirely on the retired qwen3 model.
        model_balanced=model_balanced,
    )
    drive_act = DriveActivities(
        gmail_token_dir=getattr(settings, "gmail_token_dir", "config/"),
        db_pool=deps.pool,
        knowledge_connector=connectors.get("knowledge"),
    )
    memory_act = MemoryActivities(db_pool=deps.pool)
    raindrop_act = RaindropActivities(
        raindrop_api_token=getattr(settings, "raindrop_api_token", ""),
        db_pool=deps.pool,
    )
    rss_act = RssActivities(db_pool=deps.pool)
    intel_scan_act = IntelScanActivities(searxng_url=getattr(settings, "searxng_url", ""))
    sentry_project_ids: list[int] = []
    for _p in (getattr(settings, "sentry_projects", "") or "").split(","):
        _p = _p.strip()
        if not _p:
            continue
        if _p.isdigit():
            sentry_project_ids.append(int(_p))
        else:
            logger.warning("sentry_projects_invalid_entry", value=_p)
    sentry_ingest_act = SentryIngestActivities(
        db_pool=deps.pool,
        sentry_url=getattr(settings, "sentry_url", ""),
        sentry_token=getattr(settings, "sentry_token", ""),
        sentry_org=getattr(settings, "sentry_org", ""),
        sentry_projects=sentry_project_ids,
    )
    inventory_act = InventoryActivities(
        db_pool=deps.pool,
        remote_script=connectors.get("remote_script"),
        vercel_token=getattr(settings, "vercel_token", ""),
        vercel_team_id=getattr(settings, "vercel_team_id", ""),
    )
    from aegis.connectors.todoist import TodoistConnector

    # Settings-row invariant check: the GTD pipeline (capture → clarify) reads
    # several kill switches + ids from the `settings` table that are seeded
    # via migrations 011/012. A failed migration leaves them absent and the
    # `_settings_bool(..., default=True)` calls silently engage defaults.
    # Warn loudly at boot so the operator can spot it.
    async with deps.pool.acquire() as _conn:
        _seeded = await _conn.fetch(
            "SELECT key FROM settings WHERE key = ANY($1::text[])",
            [
                "todoist_capture_enabled",
                "todoist_managed_project_ids",
                "gtd_clarify_enabled",
                "gtd_2min_rule_enabled",
                "user_timezone",
            ],
        )
    _seeded_keys = {r["key"] for r in _seeded}
    _missing_keys = {
        "todoist_capture_enabled",
        "gtd_clarify_enabled",
        "gtd_2min_rule_enabled",
        "user_timezone",
    } - _seeded_keys
    if _missing_keys:
        # `todoist_managed_project_ids` is created lazily by bootstrap_if_empty
        # so we don't include it in the kill-switch invariant.
        structlog.get_logger().warning(
            "todoist_settings_missing",
            keys=sorted(_missing_keys),
            note="defaults will engage; expected migrations 011/012 to seed them",
        )

    # timeout=10.0 keeps the httpx budget inside the activity's TIMEOUT_FAST=15s
    # window so Temporal doesn't cancel the activity mid-httpx-call.
    from aegis.services.todoist_config import resolve_todoist_api_key

    _todoist_key = await resolve_todoist_api_key(deps.pool, settings)
    todoist_connector = (
        TodoistConnector(api_key=_todoist_key, db_pool=deps.pool, timeout=10.0)
        if _todoist_key
        else None
    )
    todoist_act = TodoistActivities(
        db_pool=deps.pool,
        connector=todoist_connector,
        seed_dir=settings.seed_dir,
    )
    capture_act = CaptureActivities(
        db_pool=deps.pool,
        connector=todoist_connector,
    )
    social_act = SocialActivities(
        db_pool=deps.pool,
        connector=connectors.get("social"),
    )
    # AlertInvestigationFlow posts start- and final-comments on the Todoist
    # track-task via alert_act.post_task_note. The dataclass declared
    # todoist_connector=None upstream; wire the live connector now.
    alert_act.todoist_connector = todoist_connector
    # HomelabActivities.alert_comms_inbound_down creates Todoist tasks;
    # wire the connector now (after it's been instantiated above).
    if homelab_act is not None:
        homelab_act.todoist_connector = todoist_connector
    clarify_act = ClarifyActivities(
        db_pool=deps.pool,
        todoist_connector=todoist_connector,
        llm_client=deps.llm,
        knowledge_connector=connectors.get("knowledge"),
        # references-as-knowledge: raphael's per-message chat path
        # (filed / demoted) talks to the comms delivery server via
        # DeliveryActivities.
        delivery_connector=delivery_act,
        primary_model=model_balanced,
    )
    review_act = ReviewActivities(
        db_pool=deps.pool,
        temporal_host=getattr(settings, "temporal_host", None),
        llm_client=deps.llm,
        todoist_connector=todoist_connector,
        frame_model=getattr(settings, "model_balanced", "gpt-oss:20b"),
    )
    chat_act = ChatActivities(
        client=CoreClient(
            base_url=getattr(settings, "core_api_url", "http://localhost:8080"),
            api_key=getattr(settings, "api_key", ""),
            # ChatActivities.synthesize_reply covers smart-tier agents
            # (pandoras-actor on claude-sonnet) with heavy tool calls —
            # remote_script kimi SSH, deep KS search — that legitimately
            # take 3-6 min wall time. Aligns below the activity-level
            # TIMEOUT_CHAT_REPLY (600s) with headroom; the chat-reply
            # path uses the same 600s ceiling.
            timeout=550,
        )
    )

    # All activities
    activities = [
        active_work_act.check_active_work,
        alert_governance_act.check_alert_mute,
        alert_governance_act.write_alert_mute,
        alert_governance_act.stage_pending_pr,
        alert_governance_act.create_github_pr,
        alert_act.check_dedup,
        alert_act.find_open_task_for_signature,
        alert_act.record_signature_recurrence,
        alert_act.record_signature_new_task,
        alert_act.investigate,
        alert_act.gather_alert_knowledge,
        alert_act.log_alert,
        alert_act.resolve_infra_resource,
        alert_act.remediate_infra_service,
        alert_act.resolve_alert_resource,
        alert_act.score_resource_relevance,
        alert_act.reresolve_with_hint,
        alert_act.check_alert_resolved,
        alert_act.get_verification_delay,
        alert_act.run_investigation,
        alert_act.assess_investigation,
        alert_act.accumulate_digest_item,
        alert_act.build_alert_digest,
        alert_act.post_task_note,
        alert_act.record_verdict_to_kg,
        alert_act.upload_kimi_log,
        briefing_act.gather_calendar_events,
        briefing_act.gather_intelligence_summary,
        briefing_act.gather_references_filed,
        briefing_act.ingest_briefing,
        briefing_act.gather_market_data,
        briefing_act.format_market_section,
        briefing_act.gather_briefing_changes,
        briefing_act.frame_briefing,
        briefing_act.commit_briefing_state,
        delivery_act.send_message,
        delivery_act.send_document,
        delivery_act.send_system_event,
        delivery_act.send_voice,
        delivery_act.send_interaction_card,
        content_act.process_content,
        content_act.ingest_content,
        intel_act.dedup_items,
        intel_act.score_significance,
        intel_act.ingest_intelligence,
        cleanup_act.prune_old_records,
        cleanup_act.archive_orphan_interactions,
        cleanup_act.cleanup_old_dispatches,
        interaction_act.insert_interaction,
        interaction_act.resolve_interaction,
        interaction_act.apply_interaction_timeout,
        interaction_act.update_interaction_message_id,
        interaction_act.update_interaction_delivery_ref,
        run_recorder_act.record_workflow_run,
        channel_act.list_active_channels,
        channel_act.update_channel_config_key,
        channel_act.ingest_idempotency_claim,
        calendar_act.fetch_events,
        calendar_act.events_to_content,
        gmail_act.fetch_emails,
        gmail_act.fetch_thread,
        gmail_act.classify_email,
        gmail_act.record_triage_outcome,
        gmail_act.ingest_email_to_kg,
        gmail_act.gather_email_context,
        gmail_act.apply_label,
        drive_act.sync_drive_folder,
        memory_act.prune_agent_memories,
        raindrop_act.poll_bookmarks,
        rss_act.fetch_feed,
        intel_scan_act.search_source,
        sentry_ingest_act.fetch_new_issues,
        sentry_ingest_act.issue_to_alert,
        sentry_ingest_act.read_sentry_cursor,
        sentry_ingest_act.write_sentry_cursor,
        todoist_act.bootstrap_if_empty,
        todoist_act.fetch_sync,
        todoist_act.apply_sync_diff,
        todoist_act.drain_outbox,
        capture_act.capture_to_inbox,
        social_act.find_due_posts,
        social_act.enqueue_outbox,
        social_act.drain_social_outbox,
        social_act.complete_posted_tasks,
        social_act.unpublish_task,
        social_act.apply_social_approval,
        social_act.refresh_post_metrics,
        clarify_act.find_unclassified_items,
        clarify_act.classify_one,
        clarify_act.apply_outcome,
        clarify_act.log_classification,
        clarify_act.apply_clarify_resolution,
        clarify_act.ingest_reference_to_ks,
        clarify_act.complete_reference_task,
        clarify_act.reclassify_reference_to_reading,
        clarify_act.post_agent_reply_comment,
        clarify_act.post_agent_reply_error_comment,
        clarify_act.clear_clarify_watermark,
        chat_act.synthesize_reply,
        review_act.gather_daily_digest,
        review_act.gather_weekly_digest,
        review_act.log_review_digest,
        review_act.apply_review_acknowledgement,
        review_act.gather_weekly_state,
        review_act.frame_review,
        review_act.apply_review_decision,
        review_act.gather_today_focus,
        inventory_act.scan_workspace_repos,
        inventory_act.reconcile_workspace_resources,
        inventory_act.mirror_workspace_repos,
        inventory_act.list_vercel_projects,
        inventory_act.upsert_resources_batch,
    ]

    if settings.homelab_enabled and homelab_act is not None:
        activities += [
            homelab_act.persist_drifts,
            homelab_act.resolve_stale_drifts,
            homelab_act.notify_drift,
            homelab_act.notify_pr_event,
            homelab_act.find_undelivered_interactions,
            homelab_act.notify_undelivered_interactions,
            homelab_act.check_comms_inbound_health,
            homelab_act.alert_comms_inbound_down,
            homelab_act.collect_services,
            homelab_act.probe_and_upsert_cert,
            homelab_act.notify_cert_alert,
        ]

    if money_act:
        activities += [
            money_act.store_receipt_email,
            money_act.load_receipts,
            money_act.classify_and_extract,
            money_act.upsert_charges,
            money_act.detect_cancellations,
            money_act.evaluate_renewal_alerts,
            money_act.notify_renewal_alert,
            money_act.notify_cancellation,
            money_act.build_subscription_digest,
            money_act.notify_subscription_digest,
        ]

    # All workflows
    workflows = [
        AgentChatReplyFlow,
        AlertInvestigationFlow,
        CalendarIngestFlow,
        DailyBriefingFlow,
        CleanupFlow,
        InteractionFlow,
        GmailIngestFlow,
        GitHubAlertFlow,
        RaindropIngestFlow,
        RssIngestFlow,
        DriveSyncFlow,
        MemoryReflectionFlow,
        IntelligenceScanFlow,
        SentryPollFlow,
        TodoistSyncFlow,
        ClarifyFlow,
        DailyReviewFlow,
        WeeklyReviewFlow,
        WorkspaceRepoSyncFlow,
        VercelProjectSyncFlow,
        SocialPublishFlow,
        SocialMetricsFlow,
    ]

    if settings.homelab_enabled:
        workflows += [
            ServiceDriftFlow,
            CertRadarFlow,
            DeliveryWatchdogFlow,
        ]

    if settings.money_hygiene_enabled:
        workflows += [
            ReceiptIngestFlow,
            MoneyHygieneDailyFlow,
            SubscriptionAuditFlow,
            MoneyProcessFlow,
        ]

    # Publish final lists to module-level names so tests can inspect them
    # without running main() (the module-level stubs cover import-time tests;
    # these updates give integration-level tests the live bound methods).
    import aegis_worker.__main__ as _self  # noqa: PLW0406 — intentional self-ref

    _self.WORKFLOWS = workflows
    _self.ACTIVITIES = activities

    # Sync schedules from activities table
    sync_count = 0
    try:
        sync_count = await sync_schedules(client, deps.pool, TASK_QUEUE, settings=settings)
        logger.info("schedules_synced", count=sync_count)
    except Exception as exc:
        logger.warning("schedule_sync_failed", error=str(exc))

    # Background periodic schedule_sync — kills cold-boot race per cmemory lesson 096fe6e2
    asyncio.create_task(
        run_periodic_schedule_sync(
            client=client,
            pool=deps.pool,
            task_queue=TASK_QUEUE,
            settings=settings,
            interval_seconds=300.0,
        )
    )
    logger.info("periodic_schedule_sync_started", interval_seconds=300)

    # Start worker
    # TracingInterceptor propagates OTel context across workflow/activity
    # boundaries so a Comms → Core → Worker waterfall stays connected.
    #
    # max_concurrent_activities=10: backstop against infra-alert storms.
    # alertmanager mints a fresh fingerprint per (alertname, instance), so a
    # single outage (N nodes/services down) can dispatch N concurrent
    # AlertInvestigationFlows all hammering the LiteLLM proxy simultaneously
    # (whose backends may be the same infra that's down). Capping at 10
    # queues bursts rather than letting them saturate the proxy. The signature
    # dedup in the flow (build_alert_signature / find_open_task_for_signature)
    # is the primary storm-collapse fix; this cap is a safety net for bursts
    # that arrive before dedup fires.
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=workflows,
        activities=activities,
        interceptors=[TracingInterceptor(), WorkflowRunRecorderInterceptor()],
        max_concurrent_activities=10,
    )
    logger.info(
        "worker_starting", task_queue=TASK_QUEUE, flows=len(workflows), activities=len(activities)
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
