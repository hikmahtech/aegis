"""Mapper unit tests for schedule_sync._ACTIVITY_TYPE_MAP.

PascalCase workflow_type keys resolve to the right flow class + config
dataclass. Guards against drift between seed rows and the mapper table.
"""

from __future__ import annotations

from aegis_worker.flows.cert_radar import CertRadarConfig, CertRadarFlow
from aegis_worker.flows.daily_briefing import DailyBriefingConfig, DailyBriefingFlow
from aegis_worker.flows.money_hygiene import MoneyHygieneConfig, MoneyHygieneDailyFlow
from aegis_worker.flows.service_drift import ServiceDriftConfig, ServiceDriftFlow
from aegis_worker.flows.subscription_audit import (
    SubscriptionAuditConfig,
    SubscriptionAuditFlow,
)
from aegis_worker.flows.workspace_repo_sync import WorkspaceRepoSyncFlow, WorkspaceRepoSyncInput
from aegis_worker.schedule_sync import _ACTIVITY_TYPE_MAP


def _act(slug: str, workflow_type: str, config: dict) -> dict:
    return {
        "slug": slug,
        "workflow_type": workflow_type,
        "agent_id": "maou",
        "schedule_cron": "0 0 * * *",
        "config": config,
        "_settings": {"aegis_ui_url": ""},
    }


def test_money_hygiene_flow_mapper_resolves():
    mapper = _ACTIVITY_TYPE_MAP["MoneyHygieneDailyFlow"]
    workflow_cls, cfg = mapper(
        _act(
            "money-hygiene-daily",
            "MoneyHygieneDailyFlow",
            {"threshold_multiplier": 3.5, "thresholds_days": [30, 14, 7, 0]},
        )
    )
    assert workflow_cls is MoneyHygieneDailyFlow
    assert isinstance(cfg, MoneyHygieneConfig)
    assert cfg.thresholds_days == [30, 14, 7, 0]
    assert cfg.threshold_multiplier == 3.5
    assert cfg.silent is False


def test_money_hygiene_flow_mapper_uses_defaults():
    mapper = _ACTIVITY_TYPE_MAP["MoneyHygieneDailyFlow"]
    _, cfg = mapper(_act("money-hygiene-daily", "MoneyHygieneDailyFlow", {}))
    assert cfg.thresholds_days == [30, 14, 7, 0]
    assert cfg.threshold_multiplier == 2.0


def test_subscription_audit_flow_mapper_resolves():
    mapper = _ACTIVITY_TYPE_MAP["SubscriptionAuditFlow"]
    workflow_cls, cfg = mapper(
        _act("subscription-audit-monthly", "SubscriptionAuditFlow", {})
    )
    assert workflow_cls is SubscriptionAuditFlow
    assert isinstance(cfg, SubscriptionAuditConfig)
    assert cfg.silent is False


def test_service_drift_flow_mapper_resolves():
    mapper = _ACTIVITY_TYPE_MAP["ServiceDriftFlow"]
    workflow_cls, cfg = mapper(_act("service-drift-4h", "ServiceDriftFlow", {}))
    assert workflow_cls is ServiceDriftFlow
    assert isinstance(cfg, ServiceDriftConfig)
    assert cfg.silent is False


def test_cert_radar_flow_mapper_resolves():
    domains = ["example.com", "aegis-api.example.com"]
    mapper = _ACTIVITY_TYPE_MAP["CertRadarFlow"]
    workflow_cls, cfg = mapper(
        _act("cert-radar-daily", "CertRadarFlow", {"domains": domains})
    )
    assert workflow_cls is CertRadarFlow
    assert isinstance(cfg, CertRadarConfig)
    assert cfg.domains == domains


def test_daily_briefing_flow_mapper_resolves() -> None:
    """DailyBriefingFlow is now keyed by its PascalCase class name, consistent
    with every other flow in _ACTIVITY_TYPE_MAP.  The legacy 'briefing' key
    was removed when daily-briefing-raphael's seed row was normalized to
    workflow_type='DailyBriefingFlow'.
    """
    mapper = _ACTIVITY_TYPE_MAP["DailyBriefingFlow"]
    workflow_cls, cfg = mapper(
        {
            "slug": "daily-briefing-raphael",
            "workflow_type": "DailyBriefingFlow",
            "agent_id": "raphael",
            "schedule_cron": "30 4 * * *",
            "config": {},
            "_settings": {"aegis_ui_url": ""},
        }
    )
    assert workflow_cls is DailyBriefingFlow
    assert isinstance(cfg, DailyBriefingConfig)
    assert cfg.agent_id == "raphael"


def test_delivery_watchdog_mapper_threads_comms_url():
    """Regression: without comms_url threaded from settings, the
    watchdog's polling-health check runs with comms_url="" and is
    permanently disabled in prod."""
    from aegis_worker.flows.delivery_watchdog import (
        DeliveryWatchdogConfig,
        DeliveryWatchdogFlow,
    )

    mapper = _ACTIVITY_TYPE_MAP["DeliveryWatchdogFlow"]
    act = _act("delivery-watchdog-hourly", "DeliveryWatchdogFlow", {})
    act["_settings"]["comms_url"] = "http://aegis_comms:8081"
    workflow_cls, cfg = mapper(act)
    assert workflow_cls is DeliveryWatchdogFlow
    assert isinstance(cfg, DeliveryWatchdogConfig)
    assert cfg.comms_url == "http://aegis_comms:8081"


def test_workspace_repo_sync_flow_mapper_resolves():
    mapper = _ACTIVITY_TYPE_MAP["WorkspaceRepoSyncFlow"]
    workflow_cls, cfg = mapper(
        _act(
            "workspace-repo-sync-daily",
            "WorkspaceRepoSyncFlow",
            {"min_repos": 8},
        )
    )
    assert workflow_cls is WorkspaceRepoSyncFlow
    assert isinstance(cfg, WorkspaceRepoSyncInput)
    assert cfg.min_repos == 8


def test_workspace_repo_sync_flow_mapper_defaults():
    mapper = _ACTIVITY_TYPE_MAP["WorkspaceRepoSyncFlow"]
    _, cfg = mapper(_act("ws-sync", "WorkspaceRepoSyncFlow", {}))
    assert cfg.min_repos == 5
