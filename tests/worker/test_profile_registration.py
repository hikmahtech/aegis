"""ProfileActivities carry @activity.defn so worker registration picks them up.

A1 ships them dormant (no flow, no schedule) — without the decorator the
Worker() constructor would reject them the moment A2 wires a flow.
"""

from __future__ import annotations

from aegis_worker.activities.profile import ProfileActivities


def test_profile_activities_are_activity_defs():
    for name in ("read_profile_context", "apply_profile_patch"):
        method = getattr(ProfileActivities, name)
        assert hasattr(method, "__temporal_activity_definition"), f"{name} missing @activity.defn"
