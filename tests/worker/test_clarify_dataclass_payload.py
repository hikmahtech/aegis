"""AgentChatReplyInput required-field guard.

apply_outcome builds a payload dict; ClarifyFlow constructs the input
dataclass on the spawn side. A missing field MUST raise TypeError so
silent payload corruption can't reach the workflow body.
"""

import pytest
from aegis_worker.activities.clarify import ClarifyActivities
from aegis_worker.flows.agent_chat_reply import AgentChatReplyInput


def test_required_fields_raise_typeerror_when_missing():
    with pytest.raises(TypeError):
        AgentChatReplyInput(target_agent="raphael")  # type: ignore[call-arg]


def test_construction_with_all_required_fields_succeeds():
    inp = AgentChatReplyInput(
        target_agent="raphael",
        task_id="abc",
        synthetic_user_message="hi",
        thread_id="todoist-task-abc",
    )
    assert inp.target_agent == "raphael"
    assert inp.task_id == "abc"


def test_pandora_alert_payload_applies_overrides():
    payload = ClarifyActivities._pandora_alert_payload(
        "noon is down again",
        "desc",
        "route-123",
        "123",
        alert_overrides={"source": "todoist-infra", "alertname": "NodeDown", "severity": "critical"},
    )
    alert = payload["alert"]
    assert alert["source"] == "todoist-infra"
    assert alert["severity"] == "critical"
    assert alert["labels"]["alertname"] == "NodeDown"
    assert alert["todoist_task_id"] == "123"


def test_pandora_alert_payload_defaults_unchanged_without_overrides():
    payload = ClarifyActivities._pandora_alert_payload("APP-9: thing", "d", "fp", "9")
    assert payload["alert"]["source"] == "todoist-jira"
    assert payload["alert"]["labels"]["alertname"] == "APP-9: thing"
