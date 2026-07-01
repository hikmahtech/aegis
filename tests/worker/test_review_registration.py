"""The new review activities carry @activity.defn so the worker can register them."""
from __future__ import annotations

from aegis_worker.activities.review import ReviewActivities


def test_new_review_activities_are_activity_defs():
    for name in ("gather_weekly_state", "frame_review", "apply_review_decision"):
        method = getattr(ReviewActivities, name)
        assert hasattr(method, "__temporal_activity_definition"), (
            f"{name} is missing @activity.defn"
        )
