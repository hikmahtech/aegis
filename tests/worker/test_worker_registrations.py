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
