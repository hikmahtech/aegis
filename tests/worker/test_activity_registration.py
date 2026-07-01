def test_active_work_activities_registered():
    # Importing the worker entrypoint must not raise (catches a broken
    # import/registration); the ACTIVITIES list is built at main() runtime, so
    # we assert the class exposes the activity method instead.
    import aegis_worker.__main__  # noqa: F401
    from aegis_worker.activities.active_work import ActiveWorkActivities

    assert hasattr(ActiveWorkActivities, "check_active_work")
