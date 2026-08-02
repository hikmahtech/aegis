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
from aegis_worker.activities.agent_registry import AgentRegistryActivities
from aegis_worker.activities.agent_task import AgentTaskActivities
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
from aegis_worker.activities.curiosity import CuriosityActivities
from aegis_worker.activities.daylog import DayLogActivities
from aegis_worker.activities.delivery import DeliveryActivities
from aegis_worker.activities.drive import DriveActivities
from aegis_worker.activities.expiring_items import ExpiringItemsActivities
from aegis_worker.activities.gmail import GmailActivities
from aegis_worker.activities.homelab import HomelabActivities
from aegis_worker.activities.infra_ops import InfraOpsActivities
from aegis_worker.activities.intel_scan import IntelScanActivities
from aegis_worker.activities.intelligence import IntelligenceActivities
from aegis_worker.activities.interactions import InteractionActivities
from aegis_worker.activities.inventory import InventoryActivities
from aegis_worker.activities.llm_governor import LLMGovernorActivities
from aegis_worker.activities.memory import MemoryActivities
from aegis_worker.activities.money import MoneyActivities, parse_bank_alert_senders
from aegis_worker.activities.people import PeopleActivities
from aegis_worker.activities.profile import ProfileActivities
from aegis_worker.activities.raindrop import RaindropActivities
from aegis_worker.activities.review import ReviewActivities
from aegis_worker.activities.rss import RssActivities
from aegis_worker.activities.runs_v3 import RunRecorderActivities
from aegis_worker.activities.sentry_ingest import SentryIngestActivities
from aegis_worker.activities.social import SocialActivities
from aegis_worker.activities.todoist import TodoistActivities
from aegis_worker.activities.wearable import WearableActivities
from aegis_worker.bootstrap import bootstrap
from aegis_worker.interceptors import WorkflowRunRecorderInterceptor
from aegis_worker.registry import (
    all_activity_methods,
    base_workflows,
    check_registration,
    collect_activities,
    workflows_for,
)
from aegis_worker.schedule_sync import sync_schedules

logger = structlog.get_logger()

TASK_QUEUE = "aegis-main"

# ---------------------------------------------------------------------------
# Module-level registration views — DERIVED, never hand-maintained.
#
# WORKFLOWS: the flow classes registered regardless of any feature flag.
#   main() registers `workflows_for(settings)` instead, which adds the
#   flag-gated ones; check_registration() refuses to boot if that list ever
#   disagrees with the registry (issue #188).
#
# ACTIVITIES: every @activity.defn in aegis_worker.activities, as unbound
#   functions — import-time safe, nothing is instantiated. The live list
#   handed to Worker() is built by collect_activities() inside main() from
#   the real instances, and check_registration() proves the two agree.
#
# The single source of truth for both is aegis_worker/registry.py.
# ---------------------------------------------------------------------------

WORKFLOWS: list = base_workflows()

ACTIVITIES: list = all_activity_methods()


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
        infra_cluster=getattr(settings, "infra_cluster", "") or "",
        slack_owner_member_id=getattr(settings, "slack_owner_member_id", "") or "",
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
        model_light=model_balanced,
        db_pool=deps.pool,
    )
    cleanup_act = CleanupActivities(
        db_pool=deps.pool,
        comms_url=settings.comms_url,
        api_key=settings.api_key,
    )
    interaction_act = InteractionActivities(db_pool=deps.pool)
    run_recorder_act = RunRecorderActivities(db_pool=deps.pool)
    agent_registry_act = AgentRegistryActivities(db_pool=deps.pool)
    llm_governor_act = LLMGovernorActivities(db_pool=deps.pool)

    homelab_act = None
    if settings.homelab_enabled:
        homelab_act = HomelabActivities(
            db_pool=deps.pool,
            homelab=connectors.get("homelab"),
            delivery=delivery_act,
            heartbeat_ping_url=getattr(settings, "infra_heartbeat_ping_url", "") or "",
            infra_cluster=getattr(settings, "infra_cluster", "") or "",
        )

    money_act = None
    if settings.money_hygiene_enabled:
        money_act = MoneyActivities(
            db_pool=deps.pool,
            llm=deps.llm,
            delivery=delivery_act,
            fx_rates=getattr(settings, "money_hygiene_fx_rates", {}),
            home_currency=getattr(settings, "home_currency", "INR"),
            # balanced (kimi) — the Anthropic API smart tier is reserved for chat
            extract_model=model_balanced,
            bank_alert_senders=parse_bank_alert_senders(
                getattr(settings, "bank_alert_senders", "")
            ),
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
    # apply_enabled is the environment half of A4's two-key gate: without it,
    # `dry_run: false` on /admin/flows plans and logs but never writes.
    memory_act = MemoryActivities(
        db_pool=deps.pool,
        llm_client=deps.llm,
        model=model_balanced,
        apply_enabled=bool(getattr(settings, "memory_consolidation_apply_enabled", False)),
    )
    daylog_act = DayLogActivities(
        db_pool=deps.pool,
        llm_client=deps.llm,
        model=model_balanced,
    )
    # A1 shipped the write substrate; A2 (ProfileReflectionFlow) drives it.
    profile_act = ProfileActivities(
        db_pool=deps.pool,
        llm_client=deps.llm,
        model=model_balanced,
        # A draft_review card goes out via send_interaction_card, which bypasses
        # safe_send_message — so the flow consults the notification budget
        # itself, same knobs as delivery_act and curiosity_act.
        budget_enabled=getattr(settings, "notification_budget_enabled", False),
        daily_budget=getattr(settings, "notification_daily_budget", 8),
    )
    # C2 — passive people enrichment off email/calendar. Ships dark; email only
    # ever enriches an existing person, and the calendar (creating) lane refuses
    # while owner_emails is empty rather than minting a row for the user.
    people_act = PeopleActivities(
        db_pool=deps.pool,
        enabled=getattr(settings, "people_enrichment_enabled", False),
        owner_emails=frozenset(
            e.strip()
            for e in (getattr(settings, "owner_emails", "") or "").split(",")
            if e.strip()
        ),
    )
    # A6 ships the gap detector; A7 (CuriosityCardFlow) asks the question.
    curiosity_act = CuriosityActivities(
        db_pool=deps.pool,
        llm_client=deps.llm,
        model=model_balanced,
        # Interaction cards bypass safe_send_message, so CuriosityCardFlow
        # consults the notification budget itself — same knobs as delivery_act.
        budget_enabled=getattr(settings, "notification_budget_enabled", False),
        daily_budget=getattr(settings, "notification_daily_budget", 8),
        # Google lists the calendar owner among an event's attendees, so the
        # owner's own address must never become "a stranger you keep meeting".
        # DB-backed integration config (admin Integrations page), so it takes
        # effect on a worker restart with no redeploy. Blank = no exclusion.
        owner_emails=frozenset(
            e.strip()
            for e in (getattr(settings, "owner_emails", "") or "").split(",")
            if e.strip()
        ),
    )
    raindrop_act = RaindropActivities(
        raindrop_api_token=getattr(settings, "raindrop_api_token", ""),
        db_pool=deps.pool,
    )
    rss_act = RssActivities(db_pool=deps.pool)
    # B7 — wearable vendor poll. An empty token is not an error here: the
    # activity refuses to issue a request and reports `token_missing`, which
    # the flow surfaces in result_summary.
    wearable_act = WearableActivities(
        oura_api_token=getattr(settings, "oura_api_token", ""),
        db_pool=deps.pool,
    )
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
    )
    from aegis.connectors.todoist import TodoistConnector

    # Settings-row invariant check: the GTD pipeline (capture → clarify) reads
    # several kill switches + ids from the `settings` table that are seeded
    # via migration 001_baseline.sql. A failed migration leaves them absent and
    # the `_settings_bool(..., default=True)` calls silently engage defaults.
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
            note="defaults will engage; expected migration 001_baseline.sql to seed them",
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
    agent_task_act = AgentTaskActivities(
        db_pool=deps.pool,
        todoist_connector=todoist_connector,
        remote_script=connectors.get("remote_script"),
        homelab_connector=connectors.get("homelab"),
    )
    infra_ops_act = InfraOpsActivities(homelab_connector=connectors.get("homelab"))
    expiring_items_act = ExpiringItemsActivities(db_pool=deps.pool)
    # apply_restart_approval runs as an AgentTask activity but needs the infra
    # ops. Mirrors the existing `alert_act.todoist_connector = todoist_connector`
    # late-wiring below.
    agent_task_act.infra_ops = infra_ops_act
    # resolve_task_repo's tier 2 reuses alert_act.resolve_alert_resource
    # directly (same direct-call pattern as gmail_activities.apply_label
    # below). alert_act is constructed above, well before agent_task_act.
    agent_task_act.alert_act = alert_act
    # triage_email needs GmailActivities.apply_label plus the set of accounts
    # to probe. Active email channels are the Gmail accounts to probe. Read
    # them from the channels table (kind='email', active) — config->>'label'
    # is the account label apply_label expects: arshad-personal, arshad-stpd,
    # arshad-hikmah in prod.
    agent_task_act.gmail_activities = gmail_act
    agent_task_act.gmail_accounts = [
        r["label"]
        for r in await deps.pool.fetch(
            "SELECT config->>'label' AS label FROM channels "
            "WHERE kind = 'email' AND active AND config->>'label' IS NOT NULL"
        )
    ]
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

    # All activities — every @activity.defn method of every instance below is
    # registered automatically (registry.collect_activities). Adding a method
    # to one of these classes needs no edit here; adding a new activity CLASS
    # means constructing it above and naming it here, and check_registration()
    # below refuses to boot if you forget.
    activities = collect_activities(
        active_work_act,
        agent_registry_act,
        alert_governance_act,
        alert_act,
        briefing_act,
        delivery_act,
        content_act,
        intel_act,
        cleanup_act,
        llm_governor_act,
        interaction_act,
        run_recorder_act,
        channel_act,
        calendar_act,
        gmail_act,
        drive_act,
        memory_act,
        profile_act,
        people_act,
        curiosity_act,
        daylog_act,
        raindrop_act,
        rss_act,
        wearable_act,
        intel_scan_act,
        sentry_ingest_act,
        todoist_act,
        capture_act,
        social_act,
        clarify_act,
        chat_act,
        review_act,
        inventory_act,
        agent_task_act,
        infra_ops_act,
        expiring_items_act,
        # None when their feature flag is off — collect_activities skips those.
        homelab_act,
        money_act,
    )

    # All workflows — flag-gated entries are declared in registry.FLOWS.
    workflows = workflows_for(settings)

    # Fail loudly and early: a flow/activity declared but not fully wired (or
    # wired but not declared) dies HERE, before the worker accepts any task,
    # instead of at the first schedule tick hours later.
    check_registration(
        settings=settings,
        workflows=workflows,
        activities=activities,
        seed_dir=settings.seed_dir,
    )

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
