"""schedule_sync must not create schedules for flag-gated flows when the flag
is off — otherwise they fire against a workflow type the worker never
registered (worker/__main__.py gates those registrations on the same flags).
"""

from __future__ import annotations

from dataclasses import dataclass

from aegis_worker.schedule_sync import (
    _ACTIVITY_TYPE_MAP,
    _FEATURE_FLAGGED_TYPES,
    _disabled_by_feature_flag,
)


@dataclass
class _Flags:
    homelab_enabled: bool = False
    money_hygiene_enabled: bool = False
    trading_desk_enabled: bool = False


def test_gated_type_skipped_when_flag_off():
    s = _Flags()
    # The desk has its own flag; the homelab and money lanes have no scheduled
    # flow left since the v1 removal prep (migration 054).
    assert _disabled_by_feature_flag("TradingDeskFlow", s) == "trading_desk_enabled"


def test_gated_type_allowed_when_flag_on():
    s = _Flags(trading_desk_enabled=True)
    assert _disabled_by_feature_flag("TradingDeskFlow", s) is None


def test_desk_is_not_gated_on_the_money_flag():
    s = _Flags(money_hygiene_enabled=False, trading_desk_enabled=True)
    assert _disabled_by_feature_flag("TradingDeskFlow", s) is None


def test_ungated_type_never_blocked():
    s = _Flags()  # all off
    assert _disabled_by_feature_flag("TodoistSyncFlow", s) is None
    assert _disabled_by_feature_flag("DailyBriefingFlow", s) is None
    # The delivery watchdog left the homelab flag (v1 removal prep).
    assert _disabled_by_feature_flag("DeliveryWatchdogFlow", s) is None


def test_no_settings_means_no_gate():
    # Some callers (tests) pass settings=None — behave as before, gate nothing.
    assert _disabled_by_feature_flag("TradingDeskFlow", None) is None


def test_every_flagged_type_is_a_real_workflow():
    # Guard against a rename drifting the flag map away from the mapper keys.
    for types in _FEATURE_FLAGGED_TYPES.values():
        for t in types:
            assert t in _ACTIVITY_TYPE_MAP, f"{t} not in _ACTIVITY_TYPE_MAP"
