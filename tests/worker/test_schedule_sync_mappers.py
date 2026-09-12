"""Mapper unit tests for schedule_sync._ACTIVITY_TYPE_MAP.

PascalCase workflow_type keys resolve to the right flow class + config
dataclass. Guards against drift between seed rows and the mapper table.
"""

from __future__ import annotations

from aegis_worker.flows.cert_radar import CertRadarConfig, CertRadarFlow
from aegis_worker.flows.daily_briefing import DailyBriefingConfig, DailyBriefingFlow
from aegis_worker.flows.hub_sweep import HubSweepConfig, HubSweepFlow
from aegis_worker.flows.money_brief import MoneyBriefConfig, MoneyBriefFlow
from aegis_worker.flows.month_close import MonthCloseConfig, MonthCloseFlow
from aegis_worker.flows.receipt_ingest import (
    DEFAULT_SENDER_FILTER,
    ReceiptIngestFlow,
    ReceiptIngestInput,
)
from aegis_worker.flows.service_drift import ServiceDriftConfig, ServiceDriftFlow
from aegis_worker.flows.trading_desk import TradingDeskConfig, TradingDeskFlow
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


def test_money_brief_flow_mapper_resolves():
    mapper = _ACTIVITY_TYPE_MAP["MoneyBriefFlow"]
    workflow_cls, cfg = mapper(
        _act("money-brief-weekly", "MoneyBriefFlow", {"days": 14, "silent": True})
    )
    assert workflow_cls is MoneyBriefFlow
    assert isinstance(cfg, MoneyBriefConfig)
    assert cfg.agent_id == "maou"
    assert cfg.days == 14
    assert cfg.silent is True


def test_money_brief_flow_mapper_defaults_to_a_week_and_speaks():
    """A brief nobody is sent is a brief nobody reads: `silent` defaults off."""
    mapper = _ACTIVITY_TYPE_MAP["MoneyBriefFlow"]
    _, cfg = mapper(_act("money-brief-weekly", "MoneyBriefFlow", {}))
    assert cfg.days == 7
    assert cfg.silent is False


def test_month_close_flow_mapper_resolves():
    mapper = _ACTIVITY_TYPE_MAP["MonthCloseFlow"]
    workflow_cls, cfg = mapper(_act("money-close-monthly", "MonthCloseFlow", {"silent": True}))
    assert workflow_cls is MonthCloseFlow
    assert isinstance(cfg, MonthCloseConfig)
    assert cfg.agent_id == "maou"
    assert cfg.silent is True


def test_month_close_flow_mapper_defaults():
    mapper = _ACTIVITY_TYPE_MAP["MonthCloseFlow"]
    _, cfg = mapper(_act("money-close-monthly", "MonthCloseFlow", {}))
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


def test_receipt_ingest_mapper_takes_an_explicit_sender_filter():
    """A backfill narrows the scan by overriding the sender filter and window."""
    mapper = _ACTIVITY_TYPE_MAP["ReceiptIngestFlow"]
    workflow_cls, cfg = mapper(
        _act(
            "receipt-ingest-weekly",
            "ReceiptIngestFlow",
            {
                "sender_filter": "(from:x@y.z)",
                "query_window": "after:2026/06/30",
                "sweep_limit": 200,
            },
        )
    )
    assert workflow_cls is ReceiptIngestFlow
    assert isinstance(cfg, ReceiptIngestInput)
    assert cfg.sender_filter == "(from:x@y.z)"
    assert cfg.query == "(from:x@y.z) after:2026/06/30"
    assert cfg.sweep_limit == 200


def test_receipt_ingest_mapper_falls_back_on_an_empty_sender_filter():
    """A blank `sender_filter` must NOT mean "no filter" — that query matches
    the whole mailbox, and every message in it would be fanned out to
    MoneyProcessFlow and its LLM call. Same for a blank window."""
    mapper = _ACTIVITY_TYPE_MAP["ReceiptIngestFlow"]
    _, cfg = mapper(
        _act(
            "receipt-ingest-weekly",
            "ReceiptIngestFlow",
            {"sender_filter": "", "query_window": ""},
        )
    )
    assert cfg.sender_filter == DEFAULT_SENDER_FILTER
    assert cfg.query_window == "newer_than:14d"
    assert cfg.query == f"{DEFAULT_SENDER_FILTER} newer_than:14d"


def test_receipt_ingest_mapper_defaults():
    mapper = _ACTIVITY_TYPE_MAP["ReceiptIngestFlow"]
    _, cfg = mapper(_act("receipt-ingest-weekly", "ReceiptIngestFlow", {}))
    assert cfg.sender_filter == DEFAULT_SENDER_FILTER
    assert cfg.query_window == "newer_than:14d"
    assert cfg.sweep_limit == 20


def test_trading_desk_flow_mapper_resolves():
    mapper = _ACTIVITY_TYPE_MAP["TradingDeskFlow"]
    workflow_cls, cfg = mapper(_act("trading-desk-daily", "TradingDeskFlow", {"mode": "paper"}))
    assert workflow_cls is TradingDeskFlow
    assert isinstance(cfg, TradingDeskConfig)
    assert cfg.agent_id == "maou"


def test_hub_sweep_mapper_reads_the_grouping_thresholds():
    """`HubSweepConfig` carried these two fields and the builder ignored them,
    so the only way to raise the bar on a noisy class was the verdict cache
    (#448).

    Falsifiable: drop either field from the builder and the row's value stops
    arriving.
    """
    mapper = _ACTIVITY_TYPE_MAP["HubSweepFlow"]
    workflow_cls, cfg = mapper(
        _act(
            "hub-sweep-5m",
            "HubSweepFlow",
            {"group_min_members": 5, "group_window_hours": 24, "fix_grace_hours": 3},
        )
    )
    assert workflow_cls is HubSweepFlow
    assert isinstance(cfg, HubSweepConfig)
    assert cfg.group_min_members == 5
    assert cfg.group_window_hours == 24.0
    assert cfg.fix_grace_hours == 3.0


def test_hub_sweep_mapper_leaves_the_service_defaults_alone():
    """An empty row means "I have no opinion", and 0 is how the flow says that
    to `hub_group` — never a literal threshold of zero, which would judge every
    lone problem."""
    mapper = _ACTIVITY_TYPE_MAP["HubSweepFlow"]
    _, cfg = mapper(_act("hub-sweep-5m", "HubSweepFlow", {}))
    assert cfg.group_min_members == 0
    assert cfg.group_window_hours == 0.0
    assert cfg.fix_verify_hours == 24.0
