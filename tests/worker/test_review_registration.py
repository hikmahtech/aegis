"""The new review activities carry @activity.defn so the worker can register them."""
from __future__ import annotations

from aegis_worker.activities.review import ReviewActivities
from aegis_worker.registry import expected_activity_names


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
    """An @activity.defn the worker doesn't serve fails at runtime with
    "activity type not registered", which no unit test would otherwise catch.

    This used to grep `__main__.py` for `review_act.<name>` — a per-activity
    line that no longer exists. Since D6 `registry.collect_activities` serves
    every @activity.defn of every instance main() builds, and
    `check_registration()` refuses to boot if an activity class is not among
    them (tests/worker/test_registry.py). So the question here is just whether
    each ReviewActivities activity is in the served set.
    """
    served = expected_activity_names()
    for name in dir(ReviewActivities):
        method = getattr(ReviewActivities, name)
        if not hasattr(method, "__temporal_activity_definition"):
            continue
        assert name in served, (
            f"ReviewActivities.{name} is an activity but the worker does not serve it"
        )
