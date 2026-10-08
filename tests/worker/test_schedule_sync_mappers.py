"""Mapper unit tests for schedule_sync._ACTIVITY_TYPE_MAP.

PascalCase workflow_type keys resolve to the right flow class + config
dataclass. Guards against drift between seed rows and the mapper table.
"""

from __future__ import annotations

from aegis_worker.flows.daily_briefing import DailyBriefingConfig, DailyBriefingFlow
from aegis_worker.flows.hub_sweep import HubSweepConfig, HubSweepFlow
from aegis_worker.flows.trading_desk import TradingDeskConfig, TradingDeskFlow
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
    # A row still carrying the retired fix-verification knob maps cleanly.
    assert not hasattr(cfg, "fix_grace_hours")


def test_hub_sweep_mapper_ignores_the_retired_alertmanager_keys():
    """The alertmanager reconcile left with the infra lane (a2-devops). A row
    that still names an alertmanager (migration 056 strips the keys) must not
    turn anything back on: the mapper leaves the config's defaults alone."""
    mapper = _ACTIVITY_TYPE_MAP["HubSweepFlow"]
    _, cfg = mapper(
        _act(
            "hub-sweep-5m",
            "HubSweepFlow",
            {"alertmanager_url": "http://alertmanager:9093", "alertmanager_min_uptime_seconds": 1},
        )
    )
    assert cfg.alertmanager_url == ""
    assert cfg.alertmanager_min_uptime_seconds == 900
    assert cfg.alertmanager_min_uptime_seconds == 900


def test_hub_sweep_mapper_leaves_the_service_defaults_alone():
    """An empty row means "I have no opinion", and 0 is how the flow says that
    to `hub_group` — never a literal threshold of zero, which would judge every
    lone problem."""
    mapper = _ACTIVITY_TYPE_MAP["HubSweepFlow"]
    _, cfg = mapper(_act("hub-sweep-5m", "HubSweepFlow", {}))
    assert cfg.group_min_members == 0
    assert cfg.group_window_hours == 0.0

    # A negative number means the same thing, loudly instead of silently: a
    # window of -1 hours reaches backwards from now, matches nothing, and would
    # switch grouping off with no warning anywhere.
    _, floored = mapper(
        _act("hub-sweep-5m", "HubSweepFlow", {"group_window_hours": -1, "group_min_members": -5})
    )
    assert floored.group_window_hours == 0.0
    assert floored.group_min_members == 0


def test_the_retired_lanes_have_no_mapper():
    """v1 removal prep (054): their activities rows are gone, so they have no
    schedule config; the flows stay registered for runs in flight."""
    for flow in (
        "MoneyBriefFlow", "MonthCloseFlow", "ReceiptIngestFlow", "StatementReconcileFlow",
        "ServiceDriftFlow", "CertRadarFlow", "InfraHeartbeatFlow",
        "SentryPollFlow", "JiraSyncFlow", "WorkspaceRepoSyncFlow",
    ):
        assert flow not in _ACTIVITY_TYPE_MAP, flow
