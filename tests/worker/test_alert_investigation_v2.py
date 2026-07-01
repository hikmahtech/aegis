"""Tests for new AlertActivities: check_alert_resolved, get_verification_delay,
run_investigation, assess_investigation."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from aegis_worker.activities.alerts import AlertActivities
from temporalio.testing import ActivityEnvironment


@pytest.fixture
def mock_db_pool():
    pool = AsyncMock()
    pool.fetchrow.return_value = None
    pool.execute.return_value = "OK"
    return pool


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    llm.think.return_value = {
        "response": json.dumps(
            {
                "status": "resolved",
                "root_cause": "OOM killed the process",
                "suggested_fix": "Increase memory limit to 4GB",
                "confidence": 0.9,
            }
        ),
        "model": "gemma4:e2b",
        "prompt_tokens": 200,
        "completion_tokens": 100,
    }
    return llm


@pytest.fixture
def mock_remote_script():
    rs = AsyncMock()
    rs.start_kimi_run.return_value = {
        "run_id": "run-123",
        "repo_path": "/home/user/repos/aegis",
        "output_file": "/tmp/aegis-kimi-run-run-123.jsonl",
        "status": "running",
    }
    rs.fetch_kimi_run_output.return_value = (
        '{"session_id": "sess-456", "type": "system"}\n'
        '{"type": "assistant", "message": "Investigation complete. Found root cause: service restart needed."}\n'
        "STATUS: investigated\n"
    )
    return rs


# --- check_alert_resolved ---


async def test_check_alert_resolved_found(mock_db_pool):
    """Returns resolved=True when a matching resolved alert exists in window."""
    mock_db_pool.fetchrow.return_value = {"id": "log-entry-1"}
    activities = AlertActivities(db_pool=mock_db_pool)
    env = ActivityEnvironment()
    result = await env.run(activities.check_alert_resolved, "fp-abc", 10)
    assert result["resolved"] is True
    sql = mock_db_pool.fetchrow.call_args[0][0]
    assert "resolved" in sql
    assert "alert_received" in sql


async def test_check_alert_resolved_not_found(mock_db_pool):
    """Returns resolved=False when no matching resolved alert exists."""
    mock_db_pool.fetchrow.return_value = None
    activities = AlertActivities(db_pool=mock_db_pool)
    env = ActivityEnvironment()
    result = await env.run(activities.check_alert_resolved, "fp-abc", 10)
    assert result["resolved"] is False


# --- get_verification_delay ---


async def test_get_verification_delay_service_down():
    """ServiceDown pattern gets 300s delay."""
    activities = AlertActivities()
    env = ActivityEnvironment()
    alert = {"title": "ServiceDown: aegis-core", "severity": "critical"}
    result = await env.run(activities.get_verification_delay, alert)
    assert result["delay_seconds"] == 300
    assert "reason" in result


async def test_get_verification_delay_disk_critical():
    """DiskCritical pattern gets 0s (immediate)."""
    activities = AlertActivities()
    env = ActivityEnvironment()
    alert = {"title": "DiskCritical on node-a", "severity": "critical"}
    result = await env.run(activities.get_verification_delay, alert)
    assert result["delay_seconds"] == 0


async def test_get_verification_delay_pipeline():
    """Pipeline pattern gets 600s delay."""
    activities = AlertActivities()
    env = ActivityEnvironment()
    alert = {"title": "Pipeline success rate drop", "severity": "warning"}
    result = await env.run(activities.get_verification_delay, alert)
    assert result["delay_seconds"] == 600


async def test_get_verification_delay_default():
    """Unknown pattern gets default 180s delay."""
    activities = AlertActivities()
    env = ActivityEnvironment()
    alert = {"title": "SomeRandomAlert", "severity": "warning"}
    result = await env.run(activities.get_verification_delay, alert)
    assert result["delay_seconds"] == 180


# --- run_investigation ---


_AEGIS_RESOURCE = [
    {
        "resource_id": "r1",
        "resource_title": "AEGIS",
        "resource_path": "aegis",
        "github_repo": "youruser/aegis",
        "confidence": 0.9,
    }
]


async def test_run_investigation_success(mock_remote_script):
    """Kimi investigation returns findings."""
    activities = AlertActivities(
        remote_script=mock_remote_script,
        kimi_binary="/home/user/.local/bin/kimi",
    )
    env = ActivityEnvironment()
    alert = {"title": "OOM Kill", "severity": "critical"}
    result = await env.run(
        activities.run_investigation,
        alert,
        _AEGIS_RESOURCE,
        "Check memory usage and restart if needed",
    )
    assert result["status"] == "succeeded"
    assert result["session_id"] == "sess-456"
    mock_remote_script.start_kimi_run.assert_called_once()


async def test_run_investigation_no_kimi_binary(mock_remote_script):
    """Returns failed when kimi_binary is unset (settings not configured)."""
    activities = AlertActivities(remote_script=mock_remote_script, kimi_binary="")
    env = ActivityEnvironment()
    alert = {"title": "OOM Kill", "severity": "critical"}
    result = await env.run(
        activities.run_investigation,
        alert,
        _AEGIS_RESOURCE,
        "",
    )
    assert result["status"] == "failed"
    assert "kimi" in result["output"].lower() or "binary" in result["output"].lower()
    mock_remote_script.start_kimi_run.assert_not_called()


async def test_run_investigation_no_remote():
    """Returns failed without remote_script."""
    activities = AlertActivities(remote_script=None)
    env = ActivityEnvironment()
    alert = {"title": "OOM Kill", "severity": "critical"}
    result = await env.run(
        activities.run_investigation,
        alert,
        _AEGIS_RESOURCE,
        "Check memory",
    )
    assert result["status"] == "failed"
    assert (
        "remote_script" in result["output"].lower() or "not available" in result["output"].lower()
    )


async def test_run_investigation_timeout(mock_remote_script):
    """Returns timed_out when polling exceeds max iterations."""
    mock_remote_script.fetch_kimi_run_output.return_value = None
    activities = AlertActivities(
        remote_script=mock_remote_script,
        kimi_binary="/home/user/.local/bin/kimi",
    )
    env = ActivityEnvironment()
    alert = {"title": "Slow query", "severity": "warning"}

    with patch("aegis_worker.activities.alerts.asyncio.sleep", new_callable=AsyncMock):
        result = await env.run(
            activities.run_investigation,
            alert,
            [
                {
                    "resource_id": "r1",
                    "resource_title": "Project",
                    "resource_path": "project",
                    "github_repo": "org/project",
                    "confidence": 0.9,
                }
            ],
            "Investigate slow queries",
        )
    assert result["status"] == "timed_out"


async def test_run_investigation_no_code_resource(mock_remote_script):
    """All resources without a path → no_code_resource, kimi not invoked."""
    activities = AlertActivities(
        remote_script=mock_remote_script,
        kimi_binary="/home/user/.local/bin/kimi",
    )
    env = ActivityEnvironment()
    alert = {"title": "Knowledge service down", "severity": "critical"}
    pathless = [
        {
            "resource_id": "c1",
            "resource_title": "Knowledge Service",
            "resource_path": "",
            "confidence": 0.8,
        },
        {
            "resource_id": "c2",
            "resource_title": "Example MCP",
            "resource_path": None,
            "confidence": 0.5,
        },
    ]
    result = await env.run(activities.run_investigation, alert, pathless, "")
    assert result["status"] == "no_code_resource"
    mock_remote_script.start_kimi_run.assert_not_called()


async def test_run_investigation_passes_nested_path_and_github_repo(mock_remote_script):
    """The resource's workspace-relative path goes to start_kimi_run verbatim
    (no clone_url — JIT cloning was removed), with github_repo for engine
    routing."""
    activities = AlertActivities(
        remote_script=mock_remote_script,
        kimi_binary="/home/user/.local/bin/kimi",
    )
    env = ActivityEnvironment()
    alert = {
        "title": "Dagster pipeline failed",
        "source": "alertmanager",
        "service": "",
        "severity": "critical",
    }
    resources = [
        {
            "resource_id": "r1",
            "resource_title": "Trading System Pipeline",
            "resource_path": "trading/trading-system-pipeline",
            "github_repo": "youruser/trading-system-pipeline",
            "confidence": 0.9,
        }
    ]
    await env.run(activities.run_investigation, alert, resources, "")
    mock_remote_script.start_kimi_run.assert_called_once()
    args, kwargs = mock_remote_script.start_kimi_run.call_args
    assert args[0] == "trading/trading-system-pipeline"
    assert kwargs["github_repo"] == "youruser/trading-system-pipeline"
    assert "clone_url" not in kwargs


async def test_run_investigation_skips_pathless_primary(mock_remote_script):
    """Primary connector (no path) is skipped; first repo with a path becomes primary."""
    activities = AlertActivities(
        remote_script=mock_remote_script,
        kimi_binary="/home/user/.local/bin/kimi",
    )
    env = ActivityEnvironment()
    alert = {"title": "Knowledge service down", "source": "alertmanager", "severity": "critical"}
    resources = [
        {
            "resource_id": "c1",
            "resource_title": "Knowledge Service",
            "resource_path": "",
            "confidence": 0.85,
        },
        {
            "resource_id": "r2",
            "resource_title": "Homelab GitOps",
            "resource_path": "infra-gitops",
            "github_repo": "example/infra-gitops",
            "confidence": 0.7,
        },
    ]
    await env.run(activities.run_investigation, alert, resources, "")
    mock_remote_script.start_kimi_run.assert_called_once()
    args, kwargs = mock_remote_script.start_kimi_run.call_args
    assert args[0] == "infra-gitops"
    assert kwargs["github_repo"] == "example/infra-gitops"


# --- assess_investigation ---


async def test_assess_investigation(mock_db_pool, mock_llm):
    """Haiku produces structured verdict from investigation output."""
    activities = AlertActivities(db_pool=mock_db_pool, llm_client=mock_llm)
    env = ActivityEnvironment()
    alert = {"title": "OOM Kill", "severity": "critical", "agent_id": "pandoras-actor"}
    result = await env.run(
        activities.assess_investigation,
        alert,
        "Root cause: OOM. Fix: increase memory.",
    )
    assert result["status"] == "resolved"
    assert result["root_cause"] == "OOM killed the process"
    assert result["confidence"] == 0.9


async def test_assess_investigation_invalid_status(mock_db_pool, mock_llm):
    """Invalid status falls back to actionable."""
    mock_llm.think.return_value = {
        "response": json.dumps(
            {
                "status": "banana",
                "root_cause": "Unknown",
                "suggested_fix": "None",
                "confidence": 0.5,
            }
        ),
        "model": "gemma4:e2b",
        "prompt_tokens": 100,
        "completion_tokens": 50,
    }
    activities = AlertActivities(db_pool=mock_db_pool, llm_client=mock_llm)
    env = ActivityEnvironment()
    alert = {"title": "Weird", "severity": "info"}
    result = await env.run(
        activities.assess_investigation,
        alert,
        "Something happened",
    )
    assert result["status"] == "actionable"


async def test_assess_investigation_no_llm(mock_db_pool):
    """Returns actionable fallback when no LLM."""
    activities = AlertActivities(db_pool=mock_db_pool, llm_client=None)
    env = ActivityEnvironment()
    alert = {"title": "Test", "severity": "info"}
    result = await env.run(
        activities.assess_investigation,
        alert,
        "Some output",
    )
    assert result["status"] == "actionable"
    assert "LLM not available" in result["root_cause"]


async def test_assess_investigation_inconclusive_status_accepted(mock_db_pool, mock_llm):
    """Haiku may return inconclusive when the investigation has no evidence."""
    mock_llm.think.return_value = {
        "response": json.dumps(
            {
                "status": "inconclusive",
                "root_cause": "",
                "suggested_fix": "",
                "confidence": 0.2,
            }
        ),
        "model": "gemma4:e2b",
        "prompt_tokens": 100,
        "completion_tokens": 30,
    }
    activities = AlertActivities(db_pool=mock_db_pool, llm_client=mock_llm)
    env = ActivityEnvironment()
    alert = {"title": "Some alert", "severity": "warning"}
    result = await env.run(
        activities.assess_investigation,
        alert,
        "I could not access the relevant logs.\nSTATUS: insufficient_evidence: logs unreachable",
    )
    assert result["status"] == "inconclusive"
    assert result["root_cause"] == ""
    assert result["suggested_fix"] == ""


async def test_run_investigation_prompt_includes_grounding_rules(mock_remote_script):
    """Kimi prompt must carry the anti-hallucination rules and STATUS contract."""
    activities = AlertActivities(
        remote_script=mock_remote_script,
        kimi_binary="/home/user/.local/bin/kimi",
    )
    env = ActivityEnvironment()
    alert = {
        "title": "Dagster pipeline failed",
        "source": "alertmanager",
        "severity": "critical",
        "fingerprint": "abc123",
    }
    await env.run(activities.run_investigation, alert, _AEGIS_RESOURCE, "")
    prompt_arg = mock_remote_script.start_kimi_run.call_args.args[1]
    assert "never speculate" in prompt_arg
    assert "Every claim about the system MUST come from a tool call" in prompt_arg
    assert "STATE THAT explicitly" in prompt_arg
    assert "STATUS: investigated" in prompt_arg
    assert "STATUS: insufficient_evidence" in prompt_arg
    assert "STATUS: alert_unclear" in prompt_arg
    assert "BRANCH: <repo_name>:<branch_name>" in prompt_arg


# --- Jira-source scoping (alert.source == "todoist-jira") ---


def test_kimi_output_complete_recognises_jira_status_verbs():
    """The completion detector accepts the new Jira-scoping STATUS verbs."""
    from aegis_worker.activities.alerts import _kimi_output_complete

    assert _kimi_output_complete("...summary...\nSTATUS: scoped\n") is True
    assert _kimi_output_complete("...gap...\nSTATUS: needs_human: missing context\n") is True
    assert _kimi_output_complete("...reason...\nSTATUS: out_of_scope: ops ticket\n") is True
    # Original alert verbs still match
    assert _kimi_output_complete("rca\nSTATUS: investigated\n") is True
    # No footer → not complete
    assert _kimi_output_complete("just chatter, no status line") is False


def test_kimi_output_complete_matches_stream_json_assistant_text():
    """Production kimi output wraps assistant turns in JSON; STATUS lives
    inside the JSON-encoded `text` field, so the `\\n` before it is an escape,
    NOT a real newline. Regression for the silent-detector bug caught on the
    2026-05-20 verify run: kimi wrote STATUS: scoped but the raw-text regex
    never matched because no line in the file starts with `STATUS:`."""
    from aegis_worker.activities.alerts import _kimi_output_complete

    stream = (
        '{"role":"assistant","content":[{"type":"text","text":"Reading the source files now."}]}\n'
        '{"role":"tool","content":[{"type":"text","text":"file contents..."}],"tool_call_id":"t1"}\n'
        '{"role":"assistant","content":['
        '{"type":"think","text":"I have enough evidence."},'
        '{"type":"text","text":"Affected files: foo.py:286\\n\\nNext step: align operators.\\n\\nSTATUS: scoped"}'
        "]}\n"
        "To resume this session: kimi -r abc\n"
    )
    assert _kimi_output_complete(stream) is True


def test_kimi_output_complete_ignores_status_inside_tool_results():
    """A tool message text containing the literal word 'STATUS:' must NOT
    register as kimi-complete — only assistant messages count. Otherwise we
    short-circuit polling on noisy log/file content the agent is reading."""
    from aegis_worker.activities.alerts import _kimi_output_complete

    stream = (
        '{"role":"assistant","content":[{"type":"text","text":"Let me read the file."}]}\n'
        '{"role":"tool","content":[{"type":"text","text":"STATUS: investigated"}],"tool_call_id":"t1"}\n'
    )
    assert _kimi_output_complete(stream) is False


def test_kimi_output_complete_real_prod_fixture():
    """End-to-end fixture matching the actual kimi run from the 2026-05-20
    verify (workflow `pandora-jira-6ggh7w2w8wgCxmgv-verify-1779293782`).
    Mix of think/text content blocks, tool turns interleaved, trailing
    `To resume this session` plain-text line."""
    from aegis_worker.activities.alerts import _kimi_output_complete

    lines = [
        '{"role":"assistant","content":[{"type":"text","text":"Starting investigation."}],"tool_calls":[]}',
        '{"role":"tool","content":[{"type":"text","text":"<system>97 lines</system>"},{"type":"text","text":"     1\\tfrom dataclasses import dataclass"}],"tool_call_id":"t1"}',
        '{"role":"assistant","content":[{"type":"think","text":"Need to check rank.py too."},{"type":"text","text":"Reading rank.py."}]}',
        '{"role":"tool","content":[{"type":"text","text":"rank.py contents"}],"tool_call_id":"t2"}',
        '{"role":"assistant","content":[{"type":"think","text":""},{"type":"text","text":"Audit:\\n\\n1. Test boundary values.\\n\\nSTATUS: scoped"}]}',
        "To resume this session: kimi -r 2359c664-214c-4814-9423-87d04a55745e",
    ]
    raw = "\n".join(lines) + "\n"
    assert _kimi_output_complete(raw) is True


def test_parse_kimi_branches_stream_json():
    """BRANCH: lines also live inside assistant text in stream-json mode.
    Same root cause as STATUS; verified by extracting via the helper."""
    from aegis_worker.activities.alerts import _parse_kimi_branches

    stream = (
        '{"role":"assistant","content":[{"type":"text","text":"Committed the fix.\\n\\nBRANCH: aegis:aegis-fix/fp-1\\nBRANCH: infra-gitops:aegis-fix/fp-1"}]}\n'
        "To resume this session: kimi -r xyz\n"
    )
    branches = _parse_kimi_branches(stream, primary_repo="aegis")
    assert branches == {"aegis": "aegis-fix/fp-1", "infra-gitops": "aegis-fix/fp-1"}


def test_parse_kimi_branches_plain_text_fallback():
    """Plain-text BRANCH lines (legacy / test fixtures) still parse via
    fallback when no assistant events are present in the input."""
    from aegis_worker.activities.alerts import _parse_kimi_branches

    raw = "Output\nBRANCH: aegis-fix/fp-99\nMore output\n"
    branches = _parse_kimi_branches(raw, primary_repo="aegis")
    assert branches == {"aegis": "aegis-fix/fp-99"}


def test_parse_kimi_branches_no_branch_returns_empty():
    """No BRANCH line → empty dict (informational kimi run, no fix proposed)."""
    from aegis_worker.activities.alerts import _parse_kimi_branches

    stream = '{"role":"assistant","content":[{"type":"text","text":"Just analysis.\\nSTATUS: investigated"}]}\n'
    assert _parse_kimi_branches(stream, primary_repo="aegis") == {}


async def test_run_investigation_jira_source_uses_scoping_prompt(mock_remote_script):
    """When source == todoist-jira, kimi gets a scoping prompt instead of an RCA prompt."""
    activities = AlertActivities(
        remote_script=mock_remote_script,
        kimi_binary="/home/user/.local/bin/kimi",
    )
    env = ActivityEnvironment()
    alert = {
        "title": "APP-10741: > rule emits <= in windsorization branch",
        "description": "Comparison operators differ between sorted_set and sorted_set_ric.",
        "source": "todoist-jira",
        "service": "acme",
        "severity": "normal",
        "fingerprint": "jira-12345",
    }
    await env.run(activities.run_investigation, alert, _AEGIS_RESOURCE, "")
    prompt_arg = mock_remote_script.start_kimi_run.call_args.args[1]

    # Scoping prompt shape
    assert "Scope this Jira ticket" in prompt_arg
    assert "Affected files" in prompt_arg
    assert "Suggested next step" in prompt_arg
    assert "Do NOT commit fixes" in prompt_arg
    # New STATUS verbs present, alert verbs absent
    assert "STATUS: scoped" in prompt_arg
    assert "STATUS: needs_human" in prompt_arg
    assert "STATUS: out_of_scope" in prompt_arg
    assert "STATUS: investigated" not in prompt_arg
    assert "BRANCH:" not in prompt_arg


async def test_assess_investigation_jira_scoped_maps_to_actionable(mock_db_pool, mock_llm):
    """Jira-source: Haiku returns status=actionable with scoping summary + next step."""
    mock_llm.think.return_value = {
        "response": json.dumps(
            {
                "status": "actionable",
                "root_cause": "sorted_set.py:286 uses <= where sorted_set_ric.py:265 uses <",
                "suggested_fix": "Align comparison operators in the windsorization branch",
                "confidence": 0.85,
            }
        ),
        "model": "qwen3:14b",
        "prompt_tokens": 220,
        "completion_tokens": 80,
    }
    activities = AlertActivities(db_pool=mock_db_pool, llm_client=mock_llm)
    env = ActivityEnvironment()
    alert = {"title": "APP-10741: > rule bug", "source": "todoist-jira", "severity": "normal"}
    result = await env.run(
        activities.assess_investigation,
        alert,
        "Found <= in sorted_set.py:286 vs < in sorted_set_ric.py:265\nSTATUS: scoped",
    )
    assert result["status"] == "actionable"
    assert "sorted_set" in result["root_cause"]
    assert "Align" in result["suggested_fix"]

    # Verify the Jira-aware prompt was sent, not the default alert prompt
    prompt_sent = mock_llm.think.call_args.args[0]
    assert "Jira-ticket scoping run" in prompt_sent
    assert "STATUS: scoped" in prompt_sent
    assert "STATUS: needs_human" in prompt_sent
    assert "STATUS: out_of_scope" in prompt_sent


async def test_assess_investigation_jira_needs_human_maps_to_inconclusive(mock_db_pool, mock_llm):
    """Jira-source: needs_human → inconclusive with empty root_cause + suggested_fix."""
    mock_llm.think.return_value = {
        "response": json.dumps(
            {
                "status": "inconclusive",
                "root_cause": "",
                "suggested_fix": "",
                "confidence": 0.3,
            }
        ),
        "model": "qwen3:14b",
        "prompt_tokens": 200,
        "completion_tokens": 30,
    }
    activities = AlertActivities(db_pool=mock_db_pool, llm_client=mock_llm)
    env = ActivityEnvironment()
    alert = {"title": "APP-99: vague", "source": "todoist-jira", "severity": "normal"}
    result = await env.run(
        activities.assess_investigation,
        alert,
        "Ticket text doesn't pin down a repo.\nSTATUS: needs_human: which TBGT entity?",
    )
    assert result["status"] == "inconclusive"
    assert result["root_cause"] == ""
    assert result["suggested_fix"] == ""


async def test_assess_investigation_jira_out_of_scope_maps_to_not_actionable(
    mock_db_pool, mock_llm
):
    """Jira-source: out_of_scope → not_actionable with reason in root_cause."""
    mock_llm.think.return_value = {
        "response": json.dumps(
            {
                "status": "not_actionable",
                "root_cause": "Ticket is a data-curation task, no code change required",
                "suggested_fix": "",
                "confidence": 0.9,
            }
        ),
        "model": "qwen3:14b",
        "prompt_tokens": 200,
        "completion_tokens": 50,
    }
    activities = AlertActivities(db_pool=mock_db_pool, llm_client=mock_llm)
    env = ActivityEnvironment()
    alert = {"title": "APP-1: add financial info", "source": "todoist-jira", "severity": "normal"}
    result = await env.run(
        activities.assess_investigation,
        alert,
        "Manual data entry, nothing to code.\nSTATUS: out_of_scope: data-curation ticket",
    )
    assert result["status"] == "not_actionable"
    assert "data-curation" in result["root_cause"]


async def test_assess_investigation_alert_source_keeps_original_prompt(mock_db_pool, mock_llm):
    """Non-Jira sources still see the original alert-RCA prompt."""
    activities = AlertActivities(db_pool=mock_db_pool, llm_client=mock_llm)
    env = ActivityEnvironment()
    alert = {"title": "Pipeline drop", "source": "alertmanager", "severity": "critical"}
    await env.run(activities.assess_investigation, alert, "Some output\nSTATUS: investigated")

    prompt_sent = mock_llm.think.call_args.args[0]
    assert "Jira-ticket scoping run" not in prompt_sent
    assert "Assess this alert investigation" in prompt_sent
    assert "STATUS: insufficient_evidence" in prompt_sent


def test_kimi_output_complete_matches_claude_stream_json_shape():
    """Claude Code stream-json nests the assistant payload under "message"
    ({"type":"assistant","message":{...}}); STATUS/BRANCH parsing must see
    through that wrapper. Shape captured from a real claude v2.1.175 run."""
    from aegis_worker.activities.alerts import _kimi_output_complete

    stream = (
        '{"type":"system","subtype":"init","cwd":"/tmp","session_id":"d17fbc59","tools":["Bash"]}\n'
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Reading code."}]}}\n'
        '{"type":"user","message":{"role":"user","content":[{"type":"tool_result","content":"file body"}]}}\n'
        '{"type":"assistant","message":{"role":"assistant","content":['
        '{"type":"thinking","thinking":"done"},'
        '{"type":"text","text":"Root cause: off-by-one.\\n\\nSTATUS: investigated"}]}}\n'
        '{"type":"result","subtype":"success","result":"Root cause: off-by-one.\\n\\nSTATUS: investigated","session_id":"d17fbc59"}\n'
    )
    assert _kimi_output_complete(stream) is True


def test_kimi_output_complete_ignores_claude_tool_results():
    """STATUS inside a claude tool_result (type=user event) must not count."""
    from aegis_worker.activities.alerts import _kimi_output_complete

    stream = (
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Reading logs."}]}}\n'
        '{"type":"user","message":{"role":"user","content":[{"type":"tool_result","content":"STATUS: investigated"}]}}\n'
    )
    assert _kimi_output_complete(stream) is False


def test_parse_kimi_branches_claude_stream_json_shape():
    from aegis_worker.activities.alerts import _parse_kimi_branches

    stream = (
        '{"type":"assistant","message":{"role":"assistant","content":'
        '[{"type":"text","text":"Committed.\\n\\nBRANCH: bcp:aegis-fix/fp-7\\nSTATUS: investigated"}]}}\n'
    )
    assert _parse_kimi_branches(stream, primary_repo="bcp") == {"bcp": "aegis-fix/fp-7"}
