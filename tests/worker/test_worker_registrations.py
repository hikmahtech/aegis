"""Worker registration assertions for the comment-channel.

Since D6 there is ONE declaration — `aegis_worker/registry.py` — and a boot
check that refuses to start the worker when the runtime disagrees with it
(`tests/worker/test_registry.py` covers that machinery and its falsifiability).
What is still worth asserting per feature here is that the flow is in the
registry, that its activities carry @activity.defn under the expected names,
and that its schedule mapper reads the config keys it claims to.
"""

from types import SimpleNamespace

import aegis_worker.__main__ as worker_main
from aegis_worker.registry import expected_activity_names, workflows_for

# Prod settings: both feature flags on.
_PROD = SimpleNamespace(homelab_enabled=True, money_hygiene_enabled=True)


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


def test_agent_task_sweep_flow_registered():
    from aegis_worker.flows.agent_task import AgentTaskSweepFlow

    assert AgentTaskSweepFlow in worker_main.WORKFLOWS, (
        "AgentTaskSweepFlow must be registered in worker/__main__.py WORKFLOWS list"
    )


def test_agent_task_flow_registered():
    from aegis_worker.flows.agent_task import AgentTaskFlow

    assert AgentTaskFlow in worker_main.WORKFLOWS, (
        "AgentTaskFlow must be registered in worker/__main__.py WORKFLOWS list"
    )


def test_agent_task_activities_registered():
    names = _activity_names()
    for expected in (
        "find_actionable_tasks",
        "load_task_context",
        "park_task",
        "complete_task",
        "comment",
    ):
        assert expected in names, f"{expected} must be in __main__.ACTIVITIES list"


def test_agent_task_sweep_flow_in_schedule_map():
    from aegis_worker.schedule_sync import _ACTIVITY_TYPE_MAP

    assert "AgentTaskSweepFlow" in _ACTIVITY_TYPE_MAP
    _cls, config = _ACTIVITY_TYPE_MAP["AgentTaskSweepFlow"](
        {
            "agent_id": "pandoras-actor",
            "config": {"max_tasks": 5, "cooldown_hours": 3, "max_coding": 2},
        }
    )
    assert config.agent_id == "pandoras-actor"
    assert config.max_tasks == 5
    assert config.cooldown_hours == 3
    assert config.max_coding == 2

    # The seed row ships `config: {}`, so in a real deployment the DEFAULTS are
    # what run — a builder that forgot a key would silently take the dataclass
    # default instead of the configured one, and nothing else would notice.
    _cls, config = _ACTIVITY_TYPE_MAP["AgentTaskSweepFlow"](
        {"agent_id": "pandoras-actor", "config": {}}
    )
    assert (config.max_tasks, config.cooldown_hours) == (3, 6)
    assert config.max_coding == 3
    assert config.turn_timeout_minutes == 60


def test_agent_task_registrations_reach_the_worker():
    """Before D6 this test AST-parsed main()'s hand-written `workflows`/
    `activities` literals, because the module-level lists were a parallel truth
    that could silently disagree (issue #188). Both literals are gone: the flow
    list IS `registry.workflows_for(settings)` and the activity list IS
    `collect_activities(...)` over the instances main() builds, with
    `check_registration()` refusing to boot on any disagreement (proved in
    tests/worker/test_registry.py).

    What remains worth pinning here: these twenty names — the seventeen
    agent_task registrations plus the three infra_ops ones added alongside the
    infra verb, the restart-approval hook, the email verb and the finance verb —
    are the activity names the worker serves. A rename or a dropped
    @activity.defn still breaks the flows that call them by name.

    The one-shot coding verb (`run_task_investigation` / `collect_coding_run` /
    `run_task_implementation`) is gone: the coding lane is now one persistent
    session per task, driven by the seven task-session activities below.
    """
    served = expected_activity_names(_PROD)
    registered_flows = {c.__name__ for c in workflows_for(_PROD)}

    assert "AgentTaskSweepFlow" in registered_flows
    assert "AgentTaskFlow" in registered_flows
    for expected in (
        "find_actionable_tasks",
        "load_task_context",
        "park_task",
        "complete_task",
        "comment",
        "apply_restart_approval",
        "triage_email",
        "merchant_history",
        "apply_finance_decision",
        "resolve_task_repo",
        "load_task",
        "ensure_task_session",
        "check_task_collision",
        "launch_task_turn",
        "kill_task_turn",
        "record_task_turn",
        "find_task_turns_due",
        "service_health",
        "service_logs",
        "restart_service",
    ):
        assert expected in served, f"{expected} is not an activity the worker serves"
