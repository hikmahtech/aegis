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


def test_gated_type_skipped_when_flag_off():
    s = _Flags(homelab_enabled=False, money_hygiene_enabled=False)
    assert _disabled_by_feature_flag("CertRadarFlow", s) == "homelab_enabled"
    assert _disabled_by_feature_flag("MoneyHygieneDailyFlow", s) == "money_hygiene_enabled"


def test_gated_type_allowed_when_flag_on():
    s = _Flags(homelab_enabled=True, money_hygiene_enabled=True)
    assert _disabled_by_feature_flag("CertRadarFlow", s) is None
    assert _disabled_by_feature_flag("SubscriptionAuditFlow", s) is None


def test_ungated_type_never_blocked():
    s = _Flags()  # both off
    assert _disabled_by_feature_flag("TodoistSyncFlow", s) is None
    assert _disabled_by_feature_flag("DailyBriefingFlow", s) is None


def test_no_settings_means_no_gate():
    # Some callers (tests) pass settings=None — behave as before, gate nothing.
    assert _disabled_by_feature_flag("CertRadarFlow", None) is None


def test_every_flagged_type_is_a_real_workflow():
    # Guard against a rename drifting the flag map away from the mapper keys.
    for types in _FEATURE_FLAGGED_TYPES.values():
        for t in types:
            assert t in _ACTIVITY_TYPE_MAP, f"{t} not in _ACTIVITY_TYPE_MAP"
