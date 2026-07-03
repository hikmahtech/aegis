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
        "enqueue_outbox",
        "drain_social_outbox",
        "complete_posted_tasks",
        "unpublish_task",
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
