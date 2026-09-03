"""Single source of truth for what the worker serves, plus the boot-time
completeness check that refuses to start when the declaration and the runtime
disagree.

Registering a scheduled flow used to be six hand-edits (module `WORKFLOWS`,
main()'s `workflows`, main()'s `activities`, `_ACTIVITY_TYPE_MAP`,
`_FEATURE_FLAGGED_TYPES`, a seed row) and getting five of the six right failed
at a *different* point than getting none of them right. It is now two:

1. one :class:`FlowSpec` in :data:`FLOWS` below, and
2. one seed row in ``config/seed/activities.yaml`` (only for flows that run on
   a schedule — event-driven and child workflows need nothing).

Everything else is derived from :data:`FLOWS`:

* ``__main__.WORKFLOWS`` and the list handed to ``Worker(...)``
  (:func:`base_workflows`, :func:`workflows_for`),
* ``schedule_sync._ACTIVITY_TYPE_MAP`` (:func:`activity_type_map`),
* ``schedule_sync._FEATURE_FLAGGED_TYPES`` (:func:`feature_flagged_types`).

Activities are not listed anywhere at all: :func:`collect_activities` reads
every ``@activity.defn`` method off the instances ``main()`` constructs, so a
new activity method on an existing class needs **zero** registration edits, and
a new activity *class* needs exactly one (construct it, pass it in).

:func:`check_registration` runs in ``main()`` before ``Worker(...)`` is
constructed and raises :class:`RegistrationError` — the worker never accepts a
task with a half-wired flow.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from temporalio import activity, workflow

from aegis_worker.activities.jira import DEFAULT_KEY_PATTERN
from aegis_worker.flows.agent_chat_reply import AgentChatReplyFlow
from aegis_worker.flows.agent_run import AgentRunFlow
from aegis_worker.flows.agent_task import (
    AgentTaskFlow,
    AgentTaskSweepConfig,
    AgentTaskSweepFlow,
)
from aegis_worker.flows.alert_investigation import AlertInvestigationFlow
from aegis_worker.flows.calendar_ingest import CalendarIngestFlow, CalendarIngestInput
from aegis_worker.flows.cert_radar import CertRadarConfig, CertRadarFlow
from aegis_worker.flows.clarify import ClarifyConfig, ClarifyFlow
from aegis_worker.flows.cleanup import CleanupConfig, CleanupFlow
from aegis_worker.flows.curiosity import CuriosityCardFlow, CuriosityConfig
from aegis_worker.flows.daily_briefing import DailyBriefingConfig, DailyBriefingFlow
from aegis_worker.flows.daylog import DayLogConfig, DayLogFlow
from aegis_worker.flows.delivery_watchdog import DeliveryWatchdogConfig, DeliveryWatchdogFlow
from aegis_worker.flows.drive_sync import DriveSyncFlow, DriveSyncInput
from aegis_worker.flows.expiry_radar import ExpiryRadarConfig, ExpiryRadarFlow
from aegis_worker.flows.flow_health import FlowHealthConfig, FlowHealthWatchdogFlow
from aegis_worker.flows.github_alert import GitHubAlertFlow
from aegis_worker.flows.gmail_ingest import GmailIngestFlow, GmailIngestInput
from aegis_worker.flows.infra_heartbeat import InfraHeartbeatConfig, InfraHeartbeatFlow
from aegis_worker.flows.intelligence_scan import IntelligenceScanFlow, IntelligenceScanInput
from aegis_worker.flows.interaction import InteractionFlow
from aegis_worker.flows.jira_sync import JiraSyncConfig, JiraSyncFlow
from aegis_worker.flows.llm_spend_guard import LLMSpendGuardConfig, LLMSpendGuardFlow
from aegis_worker.flows.meeting_notes import MeetingNotesFlow
from aegis_worker.flows.memory_reflection import MemoryReflectionFlow, MemoryReflectionInput
from aegis_worker.flows.money_hygiene import MoneyHygieneConfig, MoneyHygieneDailyFlow
from aegis_worker.flows.money_process import MoneyProcessFlow
from aegis_worker.flows.profile_reflection import ProfileReflectionConfig, ProfileReflectionFlow
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
from aegis_worker.flows.wearable_ingest import WearableIngestFlow, WearableIngestInput
from aegis_worker.flows.workspace_repo_sync import WorkspaceRepoSyncFlow, WorkspaceRepoSyncInput

logger = structlog.get_logger()

# An activity row as `schedule_sync` hands it to a config builder: the DB row as
# a dict, plus an injected `_settings` sub-dict for settings-derived fields.
ActivityRow = dict[str, Any]
ScheduleConfig = Callable[[ActivityRow], Any]


class RegistrationError(RuntimeError):
    """A flow/activity is declared but not fully wired (or vice versa).

    Raised at worker boot, before the Worker is constructed, so a half-wired
    flow can never silently "never schedule".
    """


@dataclass(frozen=True)
class FlowSpec:
    """One flow, declared once.

    flow
        The ``@workflow.defn`` class.
    schedule_config
        Builds the flow's config dataclass from an `activities` DB row. ``None``
        means the flow is never started by a schedule — it is a child workflow
        or is dispatched by an event (a webhook, a chat message). Such a flow
        needs no seed row.
    feature_flag
        Name of the ``Settings`` boolean that gates registration. ``None`` =
        always registered.
    """

    flow: type
    schedule_config: ScheduleConfig | None = None
    feature_flag: str | None = None

    @property
    def name(self) -> str:
        return self.flow.__name__

    @property
    def scheduled(self) -> bool:
        return self.schedule_config is not None


def _enabled(spec: FlowSpec, settings: object | None) -> bool:
    if spec.feature_flag is None:
        return True
    return bool(getattr(settings, spec.feature_flag, False))


# ---------------------------------------------------------------------------
# THE registry.  Order here is the order flows are handed to Worker(...).
# ---------------------------------------------------------------------------

FLOWS: tuple[FlowSpec, ...] = (
    FlowSpec(AgentChatReplyFlow),
    # Event-driven: dispatched by the `dispatch_agent_run` chat tool, never on
    # a schedule — so no schedule_config and no activities.yaml seed row.
    FlowSpec(AgentRunFlow),
    FlowSpec(
        AgentTaskSweepFlow,
        lambda act: AgentTaskSweepConfig(
            agent_id=act["agent_id"],
            max_tasks=int(act["config"].get("max_tasks", 3)),
            cooldown_hours=int(act["config"].get("cooldown_hours", 6)),
            max_coding=int(act["config"].get("max_coding", 3)),
            turn_timeout_minutes=int(act["config"].get("turn_timeout_minutes", 60)),
        ),
    ),
    FlowSpec(AgentTaskFlow),
    FlowSpec(AlertInvestigationFlow),
    FlowSpec(
        CalendarIngestFlow,
        lambda act: CalendarIngestInput(
            agent_id=act["agent_id"],
            horizon_days=int(act["config"].get("horizon_days", 30)),
        ),
    ),
    FlowSpec(
        DailyBriefingFlow,
        lambda act: DailyBriefingConfig(
            agent_id=act["agent_id"],
        ),
    ),
    # day_offset 0 = the date the run starts on. At the 19:00 UTC cron that is
    # the IST day that just closed (19:00 UTC = 00:30 IST), so the default
    # needs no adjustment; the knob exists for a manual backfill of an older
    # date without touching code.
    # `mode` selects daily / weekly / monthly — three schedule rows, one flow
    # class. schedule_id is the activity slug, so the rows never collide.
    FlowSpec(
        DayLogFlow,
        lambda act: DayLogConfig(
            agent_id=act["agent_id"],
            day_offset=int(act["config"].get("day_offset", 0)),
            mode=str(act["config"].get("mode", "daily")),
        ),
    ),
    FlowSpec(
        CleanupFlow,
        lambda act: CleanupConfig(
            retentions=act["config"].get("retentions") or {},
        ),
    ),
    FlowSpec(InteractionFlow),
    # v3 Phase 3 — ingest flows + learning loop.
    # workflow_type in the seed is the workflow class name (PascalCase).
    # Config fields come from settings; the seed only carries tuning knobs.
    # GmailIngestFlow/ReceiptIngestFlow receive aegis_ui_url via the
    # settings-aware builder (see act["_settings"] injection in schedule_sync).
    FlowSpec(
        GmailIngestFlow,
        lambda act: GmailIngestInput(
            agent_id=act["agent_id"],
            max_per_account=int(act["config"].get("max_per_account", 50)),
            # 7d, not 2d: the `after:<cursor>` guard means a wider window costs
            # nothing on a healthy run, but it is the only thing that recovers
            # mail missed while the flow was down or the 50/run cap truncated.
            # Anything that falls out of this window is never triaged at all.
            query=act["config"].get("query", "is:unread newer_than:7d"),
            aegis_ui_url=act["_settings"].get("aegis_ui_url", ""),
        ),
    ),
    FlowSpec(GitHubAlertFlow),
    FlowSpec(
        RaindropIngestFlow,
        lambda act: RaindropIngestInput(agent_id=act["agent_id"]),
    ),
    FlowSpec(
        RssIngestFlow,
        lambda act: RssIngestInput(agent_id=act["agent_id"]),
    ),
    # B7 — wearable vendor poll. Not feature-flagged: the flow is inert until
    # a `channels` row with kind='wearable' is activated (the seed row ships
    # inactive), and it reports `status=no_channel` while that is the case, so
    # an unconfigured install is visible on the Flows page rather than silent.
    FlowSpec(
        WearableIngestFlow,
        lambda act: WearableIngestInput(
            agent_id=act["agent_id"],
            lookback_days=int((act["config"] or {}).get("lookback_days", 7)),
        ),
    ),
    FlowSpec(
        DriveSyncFlow,
        lambda act: DriveSyncInput(
            agent_id=act["agent_id"],
            account=(act["config"] or {}).get("account", ""),
            folder_id=(act["config"] or {}).get("folder_id", ""),
            folders=(act["config"] or {}).get("folders") or [],
            recurse=(act["config"] or {}).get("recurse", True),
            source_type=(act["config"] or {}).get("source_type", "drive"),
        ),
    ),
    # Inert until settings.llm_governor.daily_token_budget > 0 — the budget
    # lives in the settings table, not the activity config, so it can be
    # changed from the admin Settings page without touching the schedule.
    FlowSpec(
        LLMSpendGuardFlow,
        lambda act: LLMSpendGuardConfig(
            agent_id=act["agent_id"],
        ),
    ),
    FlowSpec(
        MemoryReflectionFlow,
        lambda act: MemoryReflectionInput(
            agent_id=act["agent_id"],
            keep=int((act["config"] or {}).get("keep", 50)),
            # Fail closed: an existing DB row predating A3 has neither key, and
            # `activities.config` is DB-owned (seed.py never overwrites it), so
            # enabling consolidation on a live deploy is a deliberate edit on
            # /admin/flows — not something a redeploy turns on.
            consolidate=bool((act["config"] or {}).get("consolidate", False)),
            # Every A4 rail below fails CLOSED on a missing key: a pre-A4 DB row
            # has none of them, so an existing deployment picks up dry-run,
            # the strictest quota and no hard purge without any operator action.
            dry_run=bool((act["config"] or {}).get("dry_run", True)),
            max_ops_pct=float((act["config"] or {}).get("max_ops_pct", 0.25)),
            min_age_hours=int((act["config"] or {}).get("min_age_hours", 24)),
            retire_grace_days=int((act["config"] or {}).get("retire_grace_days", 0)),
        ),
    ),
    FlowSpec(
        IntelligenceScanFlow,
        lambda act: IntelligenceScanInput(
            agent_id=act["agent_id"],
            source=act["config"].get("source", "hn"),
            topics=list(act["config"].get("topics") or []),
            max_results=int(act["config"].get("max_results", 20)),
            significance_threshold=int(act["config"].get("significance_threshold", 4)),
        ),
    ),
    FlowSpec(
        JiraSyncFlow,
        lambda act: JiraSyncConfig(
            agent_id=act["agent_id"],
            key_pattern=str(act["config"].get("key_pattern", DEFAULT_KEY_PATTERN)),
            max_tasks=int(act["config"].get("max_tasks", 100)),
            dry_run=bool(act["config"].get("dry_run", False)),
        ),
    ),
    FlowSpec(
        SentryPollFlow,
        lambda act: SentryPollInput(
            agent_id=act["agent_id"],
            mode=act["config"].get("mode", "poll"),
            limit=int(act["config"].get("limit", 25)),
        ),
    ),
    FlowSpec(
        TodoistSyncFlow,
        lambda act: TodoistSyncConfig(
            agent_id=act["agent_id"],
        ),
    ),
    FlowSpec(
        ClarifyFlow,
        lambda act: ClarifyConfig(
            agent_id=act["agent_id"],
            max_items=int((act.get("config") or {}).get("max_items") or 20),
        ),
    ),
    FlowSpec(
        DailyReviewFlow,
        lambda act: DailyReviewConfig(
            agent_id=act["agent_id"],
        ),
    ),
    FlowSpec(
        WeeklyReviewFlow,
        lambda act: WeeklyReviewConfig(
            agent_id=act["agent_id"],
        ),
    ),
    FlowSpec(
        WorkspaceRepoSyncFlow,
        lambda act: WorkspaceRepoSyncInput(
            agent_id=act["agent_id"],
            min_repos=int(act["config"].get("min_repos", 5)),
        ),
    ),
    FlowSpec(
        SocialPublishFlow,
        lambda act: SocialPublishConfig(
            agent_id=act["agent_id"],
            lookahead_minutes=int(act["config"].get("lookahead_minutes", 10)),
            default_post_hour=int(act["config"].get("default_post_hour", 9)),
            channel_sync_minutes=int(act["config"].get("channel_sync_minutes", 60)),
            max_retire=int(act["config"].get("max_retire", 20)),
        ),
    ),
    FlowSpec(
        SocialMetricsFlow,
        lambda act: SocialMetricsConfig(
            agent_id=act["agent_id"],
            window_days=int(act["config"].get("window_days", 14)),
            lookahead_days=int(act["config"].get("lookahead_days", 45)),
            max_rows=int(act["config"].get("max_rows", 200)),
            stuck_after_hours=int(act["config"].get("stuck_after_hours", 6)),
            max_stuck=int(act["config"].get("max_stuck", 50)),
            dedup_hours=int(act["config"].get("dedup_hours", 168)),
            recovery_hours=int(act["config"].get("recovery_hours", 720)),
            check_stuck=bool(act["config"].get("check_stuck", True)),
        ),
    ),
    # A7 — one curiosity question per day. aegis_ui_url comes from settings
    # (not the activity config) because cards.py renders NO button for an
    # `input` card without it, so a Slack card would otherwise be unanswerable
    # from Slack.
    FlowSpec(
        CuriosityCardFlow,
        lambda act: CuriosityConfig(
            agent_id=act["agent_id"],
            max_per_day=int(act["config"].get("max_per_day", 1)),
            limit=int(act["config"].get("limit", 5)),
            timeout_seconds=int(act["config"].get("timeout_seconds", 2 * 86400)),
            aegis_ui_url=act["_settings"].get("aegis_ui_url", ""),
        ),
    ),
    # A2 — weekly proposed edit to the agent's own `user` persona doc, delivered
    # as a draft_review card. aegis_ui_url comes from settings (not the activity
    # config) because cards.py renders NO button for `draft_review` without it,
    # so the Slack card would otherwise be a dead end.
    FlowSpec(
        ProfileReflectionFlow,
        lambda act: ProfileReflectionConfig(
            agent_id=act["agent_id"],
            lookback_days=int((act["config"] or {}).get("lookback_days", 7)),
            max_per_day=int((act["config"] or {}).get("max_per_day", 1)),
            timeout_seconds=int((act["config"] or {}).get("timeout_seconds", 7 * 86400)),
            aegis_ui_url=act["_settings"].get("aegis_ui_url", ""),
        ),
    ),
    # Life-document expiry radar (C6). Not feature-flagged — the registry is
    # empty on a fresh install, so the sweep is a no-op until the owner adds a
    # row on the admin Expiring Items page.
    FlowSpec(
        ExpiryRadarFlow,
        lambda act: ExpiryRadarConfig(
            agent_id=act["agent_id"],
            lookahead_days=int(act["config"].get("lookahead_days", 400)),
            max_cards=int(act["config"].get("max_cards", 5)),
        ),
    ),
    # Watchdog over AEGIS's own scheduled flows (#226). Deliberately NOT behind
    # homelab_enabled: it watches workflow_runs, which every install has, and
    # the silent-failure gap it closes is not homelab-specific.
    FlowSpec(
        FlowHealthWatchdogFlow,
        lambda act: FlowHealthConfig(
            agent_id=act["agent_id"],
            consecutive_failures=int((act["config"] or {}).get("consecutive_failures", 2)),
            lookback_hours=int((act["config"] or {}).get("lookback_hours", 24)),
            stale_multiplier=float((act["config"] or {}).get("stale_multiplier", 3.0)),
            min_stale_minutes=int((act["config"] or {}).get("min_stale_minutes", 60)),
            dedup_hours=int((act["config"] or {}).get("dedup_hours", 12)),
            recovery_hours=int((act["config"] or {}).get("recovery_hours", 168)),
            check_stale=bool((act["config"] or {}).get("check_stale", True)),
            check_llm=bool((act["config"] or {}).get("check_llm", True)),
            llm_consecutive=int((act["config"] or {}).get("llm_consecutive", 2)),
            llm_staleness_hours=int((act["config"] or {}).get("llm_staleness_hours", 720)),
            silent=bool((act["config"] or {}).get("silent", False)),
        ),
    ),
    # --- homelab_enabled ---------------------------------------------------
    FlowSpec(
        ServiceDriftFlow,
        lambda act: ServiceDriftConfig(
            silent=bool(act["config"].get("silent", False)),
            recheck_delay_seconds=int(act["config"].get("recheck_delay_seconds", 120)),
        ),
        feature_flag="homelab_enabled",
    ),
    FlowSpec(
        CertRadarFlow,
        lambda act: CertRadarConfig(
            silent=bool(act["config"].get("silent", False)),
            domains=act["config"].get("domains", []),
        ),
        feature_flag="homelab_enabled",
    ),
    FlowSpec(
        DeliveryWatchdogFlow,
        lambda act: DeliveryWatchdogConfig(
            silent=bool(act["config"].get("silent", False)),
            threshold_seconds=int(act["config"].get("threshold_seconds", 120)),
            window_hours=int(act["config"].get("window_hours", 24)),
            comms_url=act["_settings"].get("comms_url", ""),
        ),
        feature_flag="homelab_enabled",
    ),
    FlowSpec(
        InfraHeartbeatFlow,
        lambda act: InfraHeartbeatConfig(
            agent_id=act["agent_id"],
            fail_threshold=int(act["config"].get("fail_threshold", 3)),
            quiet_nodes=[str(n) for n in (act["config"].get("quiet_nodes") or [])],
            restuck_hours=int(act["config"].get("restuck_hours", 24)),
        ),
        feature_flag="homelab_enabled",
    ),
    # --- money_hygiene_enabled ---------------------------------------------
    FlowSpec(
        ReceiptIngestFlow,
        lambda act: ReceiptIngestInput(
            agent_id=act["agent_id"],
            max_per_account=int(act["config"].get("max_per_account", 50)),
            query_window=act["config"].get("query_window", "newer_than:14d"),
            aegis_ui_url=act["_settings"].get("aegis_ui_url", ""),
        ),
        feature_flag="money_hygiene_enabled",
    ),
    FlowSpec(
        MoneyHygieneDailyFlow,
        lambda act: MoneyHygieneConfig(
            agent_id=act["agent_id"],
            silent=bool(act["config"].get("silent", False)),
            threshold_multiplier=float(act["config"].get("threshold_multiplier", 2.0)),
            thresholds_days=act["config"].get("thresholds_days", [30, 14, 7, 0]),
        ),
        feature_flag="money_hygiene_enabled",
    ),
    FlowSpec(
        SubscriptionAuditFlow,
        lambda act: SubscriptionAuditConfig(
            agent_id=act["agent_id"],
            silent=bool(act["config"].get("silent", False)),
        ),
        feature_flag="money_hygiene_enabled",
    ),
    FlowSpec(MoneyProcessFlow, feature_flag="money_hygiene_enabled"),
    # Child of GmailIngestFlow (the `meeting` tag fan-out); never scheduled.
    FlowSpec(MeetingNotesFlow),
)

# Activity classes whose *instance* main() only builds behind a feature flag.
# Everything else in aegis_worker.activities is unconditionally served.
ACTIVITY_CLASS_FLAGS: dict[str, str] = {
    "HomelabActivities": "homelab_enabled",
    "MoneyActivities": "money_hygiene_enabled",
}


# ---------------------------------------------------------------------------
# Derived views — never hand-maintain these.
# ---------------------------------------------------------------------------


def base_workflows() -> list[type]:
    """Flow classes registered regardless of any feature flag."""
    return [s.flow for s in FLOWS if s.feature_flag is None]


def workflows_for(settings: object | None) -> list[type]:
    """The exact list handed to ``Worker(workflows=...)`` for these settings."""
    return [s.flow for s in FLOWS if _enabled(s, settings)]


def activity_type_map() -> dict[str, Callable[[ActivityRow], tuple[type, Any]]]:
    """``schedule_sync._ACTIVITY_TYPE_MAP``: workflow_type → (class, config)."""

    def _bind(flow: type, builder: ScheduleConfig):
        return lambda act: (flow, builder(act))

    return {s.name: _bind(s.flow, s.schedule_config) for s in FLOWS if s.scheduled}


def feature_flagged_types() -> dict[str, set[str]]:
    """``schedule_sync._FEATURE_FLAGGED_TYPES``: flag → gated workflow_types.

    Only *schedulable* flows appear — a flow with no config builder can never
    match an `activities` row's workflow_type, so gating it would be noise.
    """
    out: dict[str, set[str]] = {}
    for spec in FLOWS:
        if spec.feature_flag and spec.scheduled:
            out.setdefault(spec.feature_flag, set()).add(spec.name)
    return out


# ---------------------------------------------------------------------------
# Package scans — "what exists on disk", the other half of every check.
# ---------------------------------------------------------------------------


def _scan(package_name: str, predicate: Callable[[type], bool]) -> dict[str, type]:
    package = importlib.import_module(package_name)
    found: dict[str, type] = {}
    for mod_info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package_name}.{mod_info.name}")
        for name, obj in vars(module).items():
            if inspect.isclass(obj) and obj.__module__ == module.__name__ and predicate(obj):
                found[name] = obj
    return found


def _is_workflow(obj: type) -> bool:
    try:
        return workflow._Definition.from_class(obj) is not None
    except Exception:  # pragma: no cover — defensive
        return False


def activity_methods(cls: type) -> dict[str, str]:
    """``{attribute name: temporal activity name}`` for one activity class."""
    out: dict[str, str] = {}
    for attr in dir(cls):
        if attr.startswith("__"):
            continue
        try:
            defn = activity._Definition.from_callable(inspect.getattr_static(cls, attr))
        except Exception:  # pragma: no cover — defensive
            defn = None
        if defn is not None:
            out[attr] = defn.name
    return out


@functools.cache
def flow_classes() -> dict[str, type]:
    """Every ``@workflow.defn`` class under ``aegis_worker.flows``."""
    return _scan("aegis_worker.flows", _is_workflow)


@functools.cache
def activity_classes() -> dict[str, type]:
    """Every class under ``aegis_worker.activities`` owning ``@activity.defn``."""
    return _scan("aegis_worker.activities", lambda obj: bool(activity_methods(obj)))


def all_activity_methods() -> list[Callable]:
    """Every activity the worker *can* serve, as unbound functions.

    Import-time safe (nothing is instantiated). ``__main__.ACTIVITIES`` exposes
    this for registration tests; the live, feature-flag-gated list handed to
    ``Worker(...)`` is built by :func:`collect_activities` inside ``main()``.
    """
    out: list[Callable] = []
    for _, cls in sorted(activity_classes().items()):
        for attr in sorted(activity_methods(cls)):
            out.append(inspect.getattr_static(cls, attr))
    return out


def expected_activity_names(settings: object | None = None) -> set[str]:
    """Temporal activity names the worker must serve for these settings."""
    names: set[str] = set()
    for cls_name, cls in activity_classes().items():
        flag = ACTIVITY_CLASS_FLAGS.get(cls_name)
        if flag and not bool(getattr(settings, flag, False)):
            continue
        names |= set(activity_methods(cls).values())
    return names


def collect_activities(*instances: Any) -> list[Callable]:
    """Bound ``@activity.defn`` methods of every instance given (``None`` skipped).

    This is what makes activity registration declaration-free: a new activity
    method on an already-wired class is picked up with no edit here at all.
    """
    out: list[Callable] = []
    for instance in instances:
        if instance is None:
            continue
        for attr in sorted(activity_methods(type(instance))):
            out.append(getattr(instance, attr))
    return out


def seed_workflow_types(seed_dir: str | Path) -> dict[str, list[str]] | None:
    """``{workflow_type: [slug, ...]}`` from ``<seed_dir>/activities.yaml``.

    ``None`` when the file is absent — a deployment may mount a different seed
    directory, and that must not be a boot failure.
    """
    import yaml

    path = Path(seed_dir) / "activities.yaml"
    if not path.is_file():
        return None
    rows = (yaml.safe_load(path.read_text()) or {}).get("activities") or []
    out: dict[str, list[str]] = {}
    for row in rows:
        out.setdefault(row.get("workflow_type", ""), []).append(row.get("slug", "?"))
    return out


# ---------------------------------------------------------------------------
# The boot check.
# ---------------------------------------------------------------------------


def check_registration(
    settings: object | None,
    workflows: list[type],
    activities: list[Callable],
    seed_dir: str | Path | None = None,
) -> None:
    """Fail the worker boot if the declaration and the runtime disagree.

    Called from ``main()`` before ``Worker(...)``. Every check below maps to a
    real way a flow has silently failed to run in this repo:

    1. a flow declared twice, or a non-workflow, in :data:`FLOWS`,
    2. a flow written but never declared (never registered, never scheduled),
    3. the workflow list handed to ``Worker`` drifting from the registry —
       issue #188,
    4. an activity class written but never instantiated in ``main()``
       ("activity type not registered" at the first call, hours later),
    5. a schedulable flow with no seed row (it simply never runs),
    6. a seed row naming a flow with no schedule config (typo → silent no-op).

    Each is independently falsifiable; see ``tests/worker/test_registry.py``.
    """
    problems: list[str] = []

    declared = [s.name for s in FLOWS]
    duplicates = sorted({n for n in declared if declared.count(n) > 1})
    if duplicates:
        problems.append(f"declared twice in registry.FLOWS: {duplicates}")

    for spec in FLOWS:
        if not _is_workflow(spec.flow):
            problems.append(f"registry.FLOWS entry {spec.name} is not a @workflow.defn class")

    on_disk = set(flow_classes())
    undeclared = sorted(on_disk - set(declared))
    if undeclared:
        problems.append(
            f"@workflow.defn classes not declared in registry.FLOWS: {undeclared} "
            "— they would never be registered or scheduled"
        )

    expected_workflows = workflows_for(settings)
    if list(workflows) != expected_workflows:
        missing = sorted(c.__name__ for c in set(expected_workflows) - set(workflows))
        extra = sorted(c.__name__ for c in set(workflows) - set(expected_workflows))
        problems.append(
            f"workflows handed to Worker() disagree with registry.workflows_for(settings): "
            f"missing={missing} unexpected={extra}"
        )

    registered_names = {activity._Definition.must_from_callable(a).name for a in activities}
    expected_names = expected_activity_names(settings)
    missing_acts = sorted(expected_names - registered_names)
    extra_acts = sorted(registered_names - expected_names)
    if missing_acts or extra_acts:
        problems.append(
            f"activities handed to Worker() disagree with aegis_worker.activities: "
            f"missing={missing_acts} unexpected={extra_acts} — an activity class "
            "declared but not constructed in main() dies at call time with "
            "'activity type not registered'"
        )

    if seed_dir is not None:
        seed_rows = seed_workflow_types(seed_dir)
        if seed_rows is None:
            logger.warning("registration_seed_file_missing", seed_dir=str(seed_dir))
        else:
            schedulable = {s.name for s in FLOWS if s.scheduled}
            for spec in FLOWS:
                if spec.scheduled and _enabled(spec, settings) and spec.name not in seed_rows:
                    problems.append(
                        f"{spec.name} has a schedule config but no row in "
                        "config/seed/activities.yaml — nothing would ever start it"
                    )
            for wf_type, slugs in sorted(seed_rows.items()):
                if wf_type not in schedulable:
                    problems.append(
                        f"config/seed/activities.yaml rows {sorted(slugs)} name "
                        f"workflow_type={wf_type!r}, which has no schedule config in "
                        "registry.FLOWS — schedule_sync would skip them"
                    )

    if problems:
        raise RegistrationError(
            "worker registration is incomplete:\n  - " + "\n  - ".join(problems)
        )

    logger.info(
        "registration_check_ok",
        flows=len(workflows),
        activities=len(activities),
        scheduled=sum(1 for s in FLOWS if s.scheduled and _enabled(s, settings)),
    )
