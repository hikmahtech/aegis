"""The new review activities carry @activity.defn so the worker can register them."""
from __future__ import annotations

from pathlib import Path

from aegis_worker.activities.review import ReviewActivities


def test_new_review_activities_are_activity_defs():
    for name in (
        "gather_weekly_state",
        "frame_review",
        "apply_review_decision",
        "check_upcoming_key_dates",
    ):
        method = getattr(ReviewActivities, name)
        assert hasattr(method, "__temporal_activity_definition"), (
            f"{name} is missing @activity.defn"
        )


def test_every_review_activity_is_wired_into_the_worker():
    """Nothing is auto-discovered: an @activity.defn that never makes it into
    the `activities=[...]` list in __main__ fails at runtime with "activity
    type not registered", which no unit test would otherwise catch.

    The list is built inside `main()` (it needs live connectors), so it can't
    be imported — assert against the source text instead.
    """
    source = (
        Path(__file__).resolve().parents[2]
        / "worker/src/aegis_worker/__main__.py"
    ).read_text()
    for name in dir(ReviewActivities):
        method = getattr(ReviewActivities, name)
        if not hasattr(method, "__temporal_activity_definition"):
            continue
        assert f"review_act.{name}" in source, (
            f"ReviewActivities.{name} is an activity but is not registered in "
            "worker/src/aegis_worker/__main__.py"
        )
