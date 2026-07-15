"""Worker boot-time registration assertions for the comment-channel.

A new @workflow.defn / @activity.defn is INVISIBLE to Temporal unless it
appears in worker/__main__.py's explicit registration lists. These tests
catch the omission at the unit-test level instead of at "unknown workflow
type" runtime.
"""

import aegis_worker.__main__ as worker_main


def test_agent_chat_reply_flow_registered():
    from aegis_worker.flows.agent_chat_reply import AgentChatReplyFlow

    assert AgentChatReplyFlow in worker_main.WORKFLOWS, (
        "AgentChatReplyFlow must be registered in worker/__main__.py WORKFLOWS list"
    )


def _activity_names() -> list[str]:
    """Return the list of registered activity names from worker_main.ACTIVITIES.

    Temporal records activity definitions on the function via the
    @activity.defn decorator. We use temporalio.activity._Definition to
    extract the canonical name.
    """
    from temporalio import activity

    names: list[str] = []
    for a in worker_main.ACTIVITIES:
        defn = activity._Definition.must_from_callable(a)
        names.append(defn.name)
    return names


def test_agent_chat_synthesize_reply_activity_registered():
    assert "synthesize_reply" in _activity_names(), (
        "ChatActivities.synthesize_reply must be in __main__.ACTIVITIES list"
    )


def test_post_agent_reply_comment_activity_registered():
    assert "post_agent_reply_comment" in _activity_names()


def test_post_agent_reply_error_comment_activity_registered():
    assert "post_agent_reply_error_comment" in _activity_names()


def test_clear_clarify_watermark_activity_registered():
    assert "clear_clarify_watermark" in _activity_names()


def test_social_publish_flow_registered():
    from aegis_worker.flows.social_publish import SocialPublishFlow

    assert SocialPublishFlow in worker_main.WORKFLOWS, (
        "SocialPublishFlow must be registered in worker/__main__.py WORKFLOWS list"
    )


def test_social_activities_registered():
    names = _activity_names()
    for expected in (
        "find_due_posts",
        "drain_social_outbox",
        "complete_posted_tasks",
        # post_resolve hook executed BY NAME from InteractionFlow — an
        # unregistered hook fails silently at resolve time.
        "apply_social_approval",
    ):
        assert expected in names, f"{expected} must be in __main__.ACTIVITIES list"


def test_social_publish_flow_in_schedule_map():
    from aegis_worker.schedule_sync import _ACTIVITY_TYPE_MAP

    assert "SocialPublishFlow" in _ACTIVITY_TYPE_MAP
    _cls, config = _ACTIVITY_TYPE_MAP["SocialPublishFlow"](
        {"agent_id": "sebas", "config": {"lookahead_minutes": 15}}
    )
    assert config.agent_id == "sebas"
    assert config.lookahead_minutes == 15


def test_social_metrics_flow_registered():
    from aegis_worker.flows.social_metrics import SocialMetricsFlow

    assert SocialMetricsFlow in worker_main.WORKFLOWS, (
        "SocialMetricsFlow must be registered in worker/__main__.py WORKFLOWS list"
    )


def test_refresh_post_metrics_activity_registered():
    assert "refresh_post_metrics" in _activity_names(), (
        "SocialActivities.refresh_post_metrics must be in __main__.ACTIVITIES list"
    )


def test_social_metrics_flow_in_schedule_map():
    from aegis_worker.schedule_sync import _ACTIVITY_TYPE_MAP

    assert "SocialMetricsFlow" in _ACTIVITY_TYPE_MAP
    _cls, config = _ACTIVITY_TYPE_MAP["SocialMetricsFlow"](
        {"agent_id": "sebas", "config": {"window_days": 21}}
    )
    assert config.agent_id == "sebas"
    assert config.window_days == 21


def test_every_registered_activity_is_decorated():
    """Every method referenced in an activities list anywhere in __main__.py —
    including the REAL list inside main(), which no test instantiates — must
    carry @activity.defn. An undecorated entry crashes the worker at boot
    ("missing attributes, was it decorated with @activity.defn?"), which unit
    tests never see because main() only runs against live Temporal.
    """
    import ast
    import inspect

    from temporalio import activity

    src = inspect.getsource(worker_main)
    tree = ast.parse(src)

    # instance name -> Activities class name, from `x_act = SomeActivities(...)`
    instances: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
        ):
            instances[node.targets[0].id] = node.value.func.id

    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.List):
            continue
        for elt in node.elts:
            if (
                isinstance(elt, ast.Attribute)
                and isinstance(elt.value, ast.Name)
                and elt.value.id in instances
            ):
                cls = getattr(worker_main, instances[elt.value.id])
                fn = inspect.getattr_static(cls, elt.attr)
                assert activity._Definition.from_callable(fn) is not None, (
                    f"{instances[elt.value.id]}.{elt.attr} is registered in "
                    "worker/__main__.py but not decorated with @activity.defn — "
                    "the worker would crash at boot"
                )
                checked += 1
    assert checked > 50, f"AST scan found only {checked} registrations — scan is broken"
