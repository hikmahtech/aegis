"""The new briefing activities carry @activity.defn for worker registration."""
from __future__ import annotations

from aegis_worker.activities.briefing import BriefingActivities


def test_new_briefing_activities_are_activity_defs():
    for name in ("gather_briefing_changes", "frame_briefing", "commit_briefing_state"):
        method = getattr(BriefingActivities, name)
        assert hasattr(method, "__temporal_activity_definition"), f"{name} missing @activity.defn"
