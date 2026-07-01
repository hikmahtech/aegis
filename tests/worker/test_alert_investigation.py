"""Tests for AlertInvestigationFlow activities."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis_worker.activities.alerts import AlertActivities, build_alert_signature
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
        "response": (
            "Root cause: service crash due to OOM.\n"
            "Actionable: yes\n"
            "Fix: restart the service and increase memory limit.\n"
            "Can this be fixed automatically via code? yes"
        ),
        "model": "gemma4:e2b",
        "prompt_tokens": 100,
        "completion_tokens": 50,
    }
    return llm


async def test_check_dedup_no_match(mock_db_pool):
    """check_dedup returns False when no recently investigated alert exists."""
    activities = AlertActivities(db_pool=mock_db_pool)
    env = ActivityEnvironment()
    result = await env.run(activities.check_dedup, "fp-123", 24)
    assert result["is_duplicate"] is False
    sql = mock_db_pool.fetchrow.call_args[0][0]
    assert "alert_investigated" in sql


async def test_check_dedup_match(mock_db_pool):
    """check_dedup returns True when recently investigated alert exists."""
    mock_db_pool.fetchrow.return_value = {"id": "existing"}
    activities = AlertActivities(db_pool=mock_db_pool)
    env = ActivityEnvironment()
    result = await env.run(activities.check_dedup, "fp-123", 24)
    assert result["is_duplicate"] is True


async def test_check_dedup_ignores_alert_received(mock_db_pool):
    """check_dedup does NOT match on 'alert_received' action entries."""
    mock_db_pool.fetchrow.return_value = None
    activities = AlertActivities(db_pool=mock_db_pool)
    env = ActivityEnvironment()
    result = await env.run(activities.check_dedup, "fp-123", 24)
    assert result["is_duplicate"] is False
    sql = mock_db_pool.fetchrow.call_args[0][0]
    assert "alert_investigated" in sql
    assert "alert_received" not in sql


async def test_investigate_returns_assessment(mock_db_pool, mock_llm):
    """investigate calls LLM and returns actionability assessment."""
    activities = AlertActivities(db_pool=mock_db_pool, llm_client=mock_llm)
    env = ActivityEnvironment()
    alert = {"title": "HighCPU", "severity": "warning", "source": "grafana", "service": "node-a"}
    result = await env.run(activities.investigate, alert, "")
    assert "investigation" in result
    assert isinstance(result["actionable"], bool)
    assert result["actionable"] is True  # "fix" and "restart" in response


async def test_investigate_no_llm():
    """investigate returns fallback when LLM not available."""
    activities = AlertActivities(db_pool=None, llm_client=None)
    env = ActivityEnvironment()
    alert = {"title": "Test", "severity": "info"}
    result = await env.run(activities.investigate, alert, "")
    assert result["investigation"] == "LLM not available"
    assert result["actionable"] is True  # Conservative default


async def test_log_alert(mock_db_pool):
    """log_alert writes to audit_log."""
    activities = AlertActivities(db_pool=mock_db_pool)
    env = ActivityEnvironment()
    alert = {"title": "Test", "severity": "info", "source": "test", "fingerprint": "fp-test"}
    await env.run(activities.log_alert, alert)
    mock_db_pool.execute.assert_called_once()


async def test_gather_alert_knowledge_prepends_runbook(tmp_path):
    """gather_alert_knowledge returns runbook content when the file exists."""
    rb = tmp_path / "NodeDown.md"
    rb.write_text("# NodeDown\n\nSwarm node dropped off.\n\n## Diagnostic Steps\n1. Check nodes.")
    activities = AlertActivities(runbooks_dir=str(tmp_path))
    env = ActivityEnvironment()
    result = await env.run(activities.gather_alert_knowledge, "Node is down", "", "NodeDown")
    assert "Runbook:" in result
    assert "NodeDown" in result
    assert "Diagnostic Steps" in result


async def test_gather_alert_knowledge_missing_runbook_returns_empty():
    """gather_alert_knowledge returns '' when no runbook file exists — fail open."""
    activities = AlertActivities(runbooks_dir="/nonexistent/path")
    env = ActivityEnvironment()
    result = await env.run(activities.gather_alert_knowledge, "Some alert", "", "SomeAlert")
    assert result == ""


async def test_gather_alert_knowledge_stub_skipped(tmp_path):
    """gather_alert_knowledge returns '' for stub files containing 'TODO: fill in'."""
    stub = tmp_path / "NodeHighCPU.md"
    stub.write_text("# NodeHighCPU\n\nTODO: fill in")
    activities = AlertActivities(runbooks_dir=str(tmp_path))
    env = ActivityEnvironment()
    result = await env.run(activities.gather_alert_knowledge, "High CPU", "", "NodeHighCPU")
    assert result == ""


@pytest.fixture
def mock_remote_script():
    rs = AsyncMock()
    rs._repo_base = "/home/user/repos"
    rs._ssh_args = MagicMock(return_value=["ssh", "node-a", "cmd"])
    rs.start_kimi_run = AsyncMock(
        return_value={
            "status": "running",
            "run_id": "test-run",
            "repo": "aegis",
            "repo_path": "/home/user/repos/aegis",
            "output_file": "/tmp/aegis-kimi-run-test.jsonl",
        }
    )
    return rs


SAMPLE_RESOURCES = [
    {
        "resource_id": "res-001",
        "resource_title": "AEGIS",
        "resource_path": "aegis",
        "github_repo": "youruser/aegis",
        "confidence": 0.9,
    },
    {
        "resource_id": "res-003",
        "resource_title": "Homelab GitOps",
        "resource_path": "infra-gitops",
        "github_repo": "example/infra-gitops",
        "confidence": 0.8,
    },
]


async def test_run_investigation_prompt_confines_to_single_repo(
    mock_db_pool, mock_remote_script
):
    """The Kimi prompt confines to the primary repo's worktree and does NOT
    invite reading secondary repos (sandbox confinement, C)."""
    output_jsonl = (
        '{"session_id": "sess-abc"}\nBRANCH: aegis:aegis-fix/fp123\n'
        "Kimi output here\nSTATUS: investigated\n"
    )
    mock_remote_script.fetch_kimi_run_output = AsyncMock(return_value=output_jsonl)

    act = AlertActivities(
        db_pool=mock_db_pool,
        remote_script=mock_remote_script,
        kimi_binary="/usr/local/bin/kimi",
    )
    env = ActivityEnvironment()
    alert = {
        "title": "OOM",
        "severity": "critical",
        "fingerprint": "fp123",
        "source": "alertmanager",
        "description": "",
    }
    result = await env.run(act.run_investigation, alert, SAMPLE_RESOURCES, "")

    assert result["status"] == "succeeded"
    call_args = mock_remote_script.start_kimi_run.call_args
    prompt_sent = call_args[0][1]  # second positional arg is prompt
    # Confinement instruction present; secondary repo NOT invited for reading.
    assert "current working directory" in prompt_sent
    assert "infra-gitops" not in prompt_sent


async def test_run_investigation_parses_multi_branch(mock_db_pool, mock_remote_script):
    """Multiple BRANCH: lines are parsed into branches dict."""
    output_jsonl = (
        '{"session_id": "sess-abc"}\n'
        "BRANCH: aegis:aegis-fix/fp123\n"
        "BRANCH: infra-gitops:aegis-fix/fp123\n"
        "Some analysis output\n"
        "STATUS: investigated\n"
    )
    mock_remote_script.fetch_kimi_run_output = AsyncMock(return_value=output_jsonl)

    act = AlertActivities(
        db_pool=mock_db_pool,
        remote_script=mock_remote_script,
        kimi_binary="/usr/local/bin/kimi",
    )
    env = ActivityEnvironment()
    alert = {
        "title": "OOM",
        "severity": "critical",
        "fingerprint": "fp123",
        "source": "alertmanager",
        "description": "",
    }
    result = await env.run(act.run_investigation, alert, SAMPLE_RESOURCES, "")

    assert result["status"] == "succeeded"
    assert result["branches"] == {
        "aegis": "aegis-fix/fp123",
        "infra-gitops": "aegis-fix/fp123",
    }
    # backward-compat: branch = primary repo branch
    assert result["branch"] == "aegis-fix/fp123"


async def test_run_investigation_backward_compat_branch_no_repo_prefix(
    mock_db_pool, mock_remote_script
):
    """Old-style BRANCH: <name> (no repo prefix) assigns branch to primary repo."""
    output_jsonl = (
        '{"session_id": "sess-abc"}\nBRANCH: aegis-fix/fp123\nOutput\nSTATUS: investigated\n'
    )
    mock_remote_script.fetch_kimi_run_output = AsyncMock(return_value=output_jsonl)

    act = AlertActivities(
        db_pool=mock_db_pool,
        remote_script=mock_remote_script,
        kimi_binary="/usr/local/bin/kimi",
    )
    env = ActivityEnvironment()
    alert = {
        "title": "T",
        "severity": "info",
        "fingerprint": "fp123",
        "source": "github",
        "description": "",
    }
    result = await env.run(act.run_investigation, alert, SAMPLE_RESOURCES[:1], "")

    assert result["branch"] == "aegis-fix/fp123"
    assert result["branches"] == {"aegis": "aegis-fix/fp123"}


async def test_run_investigation_empty_branches_when_no_branch_line(
    mock_db_pool, mock_remote_script
):
    """When Kimi outputs no BRANCH: line, branches is empty dict."""
    output_jsonl = (
        '{"session_id": "sess-abc"}\nJust analysis, no branch created.\nSTATUS: investigated\n'
    )
    mock_remote_script.fetch_kimi_run_output = AsyncMock(return_value=output_jsonl)

    act = AlertActivities(
        db_pool=mock_db_pool,
        remote_script=mock_remote_script,
        kimi_binary="/usr/local/bin/kimi",
    )
    env = ActivityEnvironment()
    alert = {
        "title": "T",
        "severity": "info",
        "fingerprint": "fp123",
        "source": "github",
        "description": "",
    }
    result = await env.run(act.run_investigation, alert, SAMPLE_RESOURCES[:1], "")

    assert result["branches"] == {}
    assert result["branch"] == ""


async def test_run_investigation_waits_for_status_footer(
    monkeypatch, mock_db_pool, mock_remote_script
):
    """Polling continues past partial output until kimi emits its STATUS: footer.

    Regression for the bug where the activity returned `status=succeeded` on the
    first non-empty poll, capturing only ~30s of kimi work and feeding Haiku a
    truncated transcript.
    """
    partial = '{"session_id": "sess-abc"}\nKimi is still reading workflow YAML...\n'
    final = partial + "BRANCH: aegis:aegis-fix/fp123\nSTATUS: investigated\n"
    mock_remote_script.fetch_kimi_run_output = AsyncMock(side_effect=[partial, partial, final])
    monkeypatch.setattr("aegis_worker.activities.alerts.asyncio.sleep", AsyncMock())

    act = AlertActivities(
        db_pool=mock_db_pool,
        remote_script=mock_remote_script,
        kimi_binary="/usr/local/bin/kimi",
    )
    env = ActivityEnvironment()
    alert = {
        "title": "Build failed",
        "severity": "error",
        "fingerprint": "fp123",
        "source": "github",
        "description": "",
    }
    result = await env.run(act.run_investigation, alert, SAMPLE_RESOURCES[:1], "")

    assert result["status"] == "succeeded"
    assert mock_remote_script.fetch_kimi_run_output.await_count == 3
    assert result["branches"] == {"aegis": "aegis-fix/fp123"}
    assert "STATUS: investigated" in result["output"]


async def test_run_investigation_recognises_insufficient_evidence_status(
    monkeypatch, mock_db_pool, mock_remote_script
):
    """STATUS: insufficient_evidence: <reason> also counts as a final footer."""
    final = (
        '{"session_id": "sess-abc"}\n'
        "Tried gh run view but no auth token in env.\n"
        "STATUS: insufficient_evidence: GitHub auth missing\n"
    )
    mock_remote_script.fetch_kimi_run_output = AsyncMock(return_value=final)
    monkeypatch.setattr("aegis_worker.activities.alerts.asyncio.sleep", AsyncMock())

    act = AlertActivities(
        db_pool=mock_db_pool,
        remote_script=mock_remote_script,
        kimi_binary="/usr/local/bin/kimi",
    )
    env = ActivityEnvironment()
    alert = {
        "title": "T",
        "severity": "info",
        "fingerprint": "fp9",
        "source": "github",
        "description": "",
    }
    result = await env.run(act.run_investigation, alert, SAMPLE_RESOURCES[:1], "")
    assert result["status"] == "succeeded"
    assert "insufficient_evidence" in result["output"]


async def test_run_investigation_times_out_without_status_footer(
    monkeypatch, mock_db_pool, mock_remote_script
):
    """If kimi never emits STATUS:, activity returns timed_out after max_iterations.

    The timed_out result must also thread back the effective kimi host so a
    kimi_partial upload reads the output file from the host kimi ran on, not
    the base host.
    """
    no_footer = '{"session_id": "sess-x"}\nKimi just spinning, never wrote STATUS:\n'
    mock_remote_script.fetch_kimi_run_output = AsyncMock(return_value=no_footer)
    mock_remote_script.start_kimi_run = AsyncMock(
        return_value={
            "status": "running",
            "run_id": "test-run",
            "repo": "aegis",
            "repo_path": "/home/user/repos/aegis",
            "output_file": "/tmp/aegis-kimi-run-test.jsonl",
            "host": "node-b",
        }
    )
    monkeypatch.setattr("aegis_worker.activities.alerts.asyncio.sleep", AsyncMock())

    act = AlertActivities(
        db_pool=mock_db_pool,
        remote_script=mock_remote_script,
        kimi_binary="/usr/local/bin/kimi",
    )
    env = ActivityEnvironment()
    alert = {
        "title": "T",
        "severity": "info",
        "fingerprint": "fp-no-status",
        "source": "github",
        "description": "",
    }
    result = await env.run(act.run_investigation, alert, SAMPLE_RESOURCES[:1], "")
    assert result["status"] == "timed_out"
    assert result["host"] == "node-b"


# ── stream-json end-to-end through run_investigation ────────────────────


async def test_run_investigation_stream_json_succeeded_with_status_and_branches(
    monkeypatch, mock_db_pool, mock_remote_script
):
    """Realistic kimi stream-json output (assistant + tool events, STATUS
    + BRANCH inside assistant text) reaches `status=succeeded` AND extracts
    branches correctly. Regression for the silent-detector bug observed in
    prod on the 2026-05-20 verify run — kimi finished cleanly but the
    activity returned `timed_out` because the raw-text regex never matched.
    """
    stream = (
        '{"role":"assistant","content":[{"type":"text","text":"Investigating."}]}\n'
        '{"role":"tool","content":[{"type":"text","text":"Found pattern"}],"tool_call_id":"t1"}\n'
        '{"role":"assistant","content":['
        '{"type":"think","text":"Time to commit the fix."},'
        '{"type":"text","text":"Root cause confirmed.\\n\\nBRANCH: aegis:aegis-fix/fp77\\n\\nSTATUS: investigated"}'
        "]}\n"
        "To resume this session: kimi -r abc-def\n"
    )
    mock_remote_script.fetch_kimi_run_output = AsyncMock(return_value=stream)
    monkeypatch.setattr("aegis_worker.activities.alerts.asyncio.sleep", AsyncMock())

    act = AlertActivities(
        db_pool=mock_db_pool,
        remote_script=mock_remote_script,
        kimi_binary="/usr/local/bin/kimi",
    )
    env = ActivityEnvironment()
    alert = {
        "title": "OOM",
        "severity": "critical",
        "fingerprint": "fp77",
        "source": "alertmanager",
        "description": "",
    }
    result = await env.run(act.run_investigation, alert, SAMPLE_RESOURCES[:1], "")

    assert result["status"] == "succeeded"
    assert result["branches"] == {"aegis": "aegis-fix/fp77"}
    assert result["branch"] == "aegis-fix/fp77"


# ── post_task_note (Pandora ↔ Todoist comments) ────────────────────────


async def test_post_task_note_happy_path():
    """post_task_note builds a note_add command and submits via connector.

    Envelope-ok plus per-command sync_status='ok' both required — see
    test_post_task_note_envelope_ok_but_command_rejected for the silent
    failure this guards against.
    """
    connector = AsyncMock()

    async def _commands(cmds):
        return {
            "ok": True,
            "data": {
                "sync_status": {c["uuid"]: "ok" for c in cmds},
                "temp_id_mapping": {},
            },
            "error": None,
        }

    connector.commands = AsyncMock(side_effect=_commands)
    acts = AlertActivities(todoist_connector=connector)
    env = ActivityEnvironment()
    result = await env.run(acts.post_task_note, "TASK_42", "Investigation started")
    assert result["ok"] is True
    sent = connector.commands.await_args.args[0]
    assert len(sent) == 1
    assert sent[0]["type"] == "note_add"
    assert sent[0]["args"] == {"item_id": "TASK_42", "content": "Investigation started"}


async def test_post_task_note_envelope_ok_but_command_rejected():
    """Envelope-ok + per-command rejection (e.g. ITEM_NOT_FOUND on a
    deleted task) must surface as ok=False. Caught in prod 2026-05-20
    where the Sync envelope returned ok=True but every note_add was
    rejected because TodoistSyncFlow had left the local todoist_tasks
    rows stale — the items had been deleted in Todoist.
    """
    connector = AsyncMock()

    async def _commands(cmds):
        return {
            "ok": True,
            "data": {
                "sync_status": {
                    cmds[0]["uuid"]: {
                        "error": "Item not found",
                        "error_code": 22,
                        "error_tag": "ITEM_NOT_FOUND",
                        "http_code": 404,
                    }
                },
                "temp_id_mapping": {},
            },
            "error": None,
        }

    connector.commands = AsyncMock(side_effect=_commands)
    acts = AlertActivities(todoist_connector=connector)
    env = ActivityEnvironment()
    result = await env.run(acts.post_task_note, "DELETED_TASK", "Investigation started")
    assert result["ok"] is False
    assert "command_rejected" in (result["error"] or "")


async def test_post_task_note_no_connector():
    """post_task_note returns ok=False when no connector is wired."""
    acts = AlertActivities(todoist_connector=None)
    env = ActivityEnvironment()
    result = await env.run(acts.post_task_note, "TASK_42", "Hi")
    assert result["ok"] is False


async def test_post_task_note_swallows_connector_error():
    """post_task_note surfaces ok=False on connector failure, never raises."""
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": False, "error": "http_503", "retryable": True}
    )
    acts = AlertActivities(todoist_connector=connector)
    env = ActivityEnvironment()
    result = await env.run(acts.post_task_note, "TASK_42", "Hi")
    assert result["ok"] is False
    assert result["error"] == "http_503"


async def test_post_task_note_threads_file_attachment_into_command():
    """When called with a file_attachment, the resulting note_add command
    carries it in args.file_attachment so the comment renders with the
    uploaded file in Todoist."""
    connector = AsyncMock()

    async def _commands(cmds):
        return {
            "ok": True,
            "data": {
                "sync_status": {c["uuid"]: "ok" for c in cmds},
                "temp_id_mapping": {},
            },
            "error": None,
        }

    connector.commands = AsyncMock(side_effect=_commands)
    acts = AlertActivities(todoist_connector=connector)
    env = ActivityEnvironment()
    blob = {
        "file_url": "https://files.todoist.com/abc.gz",
        "file_name": "kimi-run.log.gz",
        "file_size": 4096,
        "file_type": "application/gzip",
        "upload_state": "completed",
    }
    result = await env.run(acts.post_task_note, "TASK_42", "see attached", blob)
    assert result["ok"] is True
    sent = connector.commands.await_args.args[0]
    assert sent[0]["args"]["file_attachment"] == blob


async def test_post_task_note_appends_clickable_workflow_link():
    """When workflow_id + run_id are supplied and a temporal UI url is
    configured, the comment gains a clickable `Workflow run: [id](url)` footer.
    The literal `Workflow run:` token is preserved so clarify's loop-guard SQL
    (`NOT LIKE '%Workflow run:%'`) keeps excluding these machine notes."""
    connector = AsyncMock()
    captured: dict = {}

    async def _commands(cmds):
        captured["cmds"] = cmds
        return {
            "ok": True,
            "data": {"sync_status": {c["uuid"]: "ok" for c in cmds}, "temp_id_mapping": {}},
            "error": None,
        }

    connector.commands = AsyncMock(side_effect=_commands)
    acts = AlertActivities(
        todoist_connector=connector,
        temporal_ui_url="https://temporal.example.com",
        temporal_namespace="default",
    )
    env = ActivityEnvironment()
    result = await env.run(acts.post_task_note, "TASK_42", "Verdict body", None, "wf-99", "run-77")
    assert result["ok"] is True
    content = captured["cmds"][0]["args"]["content"]
    assert content.startswith("Verdict body")
    assert "Workflow run: [wf-99]" in content
    assert (
        "https://temporal.example.com/namespaces/default/workflows/wf-99/run-77/history"
        in content
    )


async def test_post_task_note_plain_marker_when_no_ui_url():
    """No temporal UI url wired → still append the plain `Workflow run:` marker
    so the loop-guard token is present even without a clickable link."""
    connector = AsyncMock()
    captured: dict = {}

    async def _commands(cmds):
        captured["cmds"] = cmds
        return {
            "ok": True,
            "data": {"sync_status": {c["uuid"]: "ok" for c in cmds}, "temp_id_mapping": {}},
            "error": None,
        }

    connector.commands = AsyncMock(side_effect=_commands)
    acts = AlertActivities(todoist_connector=connector)  # temporal_ui_url unset
    env = ActivityEnvironment()
    await env.run(acts.post_task_note, "TASK_42", "Body", None, "wf-99", "run-77")
    content = captured["cmds"][0]["args"]["content"]
    assert "Workflow run: wf-99" in content
    assert "[wf-99]" not in content


# ── upload_kimi_log (kimi transcript → Todoist attachment) ────────────


async def test_upload_kimi_log_happy_path():
    """upload_kimi_log fetches the remote output, extracts assistant text,
    and uploads it to Todoist as a plain-text log."""
    import json as _json

    # Build a stream-json transcript with two assistant turns.
    raw = "\n".join(
        [
            _json.dumps(
                {"role": "assistant", "content": [{"type": "text", "text": "Looking at logs..."}]}
            ),
            _json.dumps(
                {"role": "tool", "content": [{"type": "tool_result", "text": "irrelevant"}]}
            ),
            _json.dumps(
                {"role": "assistant", "content": [{"type": "text", "text": "STATUS: scoped"}]}
            ),
        ]
    )
    remote = AsyncMock()
    remote.fetch_kimi_run_output = AsyncMock(return_value=raw)
    connector = AsyncMock()
    blob = {
        "file_url": "https://files.todoist.com/abc.log",
        "file_name": "kimi-x.log",
        "upload_state": "completed",
    }
    connector.upload_file = AsyncMock(return_value={"ok": True, "data": blob, "error": None})
    acts = AlertActivities(remote_script=remote, todoist_connector=connector)
    env = ActivityEnvironment()
    result = await env.run(acts.upload_kimi_log, "/tmp/kimi.out", "fp-oom-1-12345678")
    assert result["ok"] is True
    assert result["file_attachment"] == blob
    assert result["file_name"].endswith(".log")
    assert not result["file_name"].endswith(".gz")
    # Verify the uploaded body is the plain-text human-readable transcript
    # (not gzipped, not the raw stream-json).
    upload_call = connector.upload_file.await_args
    decoded = upload_call.kwargs["content"].decode("utf-8")
    assert "Looking at logs" in decoded
    assert "STATUS: scoped" in decoded
    # Tool/JSON-envelope noise must not appear in the extracted transcript
    assert "tool_result" not in decoded
    assert upload_call.kwargs["content_type"] == "text/plain"


async def test_upload_kimi_log_no_connector_or_args():
    acts = AlertActivities(remote_script=None, todoist_connector=None)
    env = ActivityEnvironment()
    result = await env.run(acts.upload_kimi_log, "", "x")
    assert result["ok"] is False
    assert result["file_attachment"] is None


async def test_upload_kimi_log_fetch_failure_returns_error():
    remote = AsyncMock()
    remote.fetch_kimi_run_output = AsyncMock(side_effect=RuntimeError("ssh dropped"))
    connector = AsyncMock()
    acts = AlertActivities(remote_script=remote, todoist_connector=connector)
    env = ActivityEnvironment()
    result = await env.run(acts.upload_kimi_log, "/tmp/k.out", "x")
    assert result["ok"] is False
    assert "fetch_failed" in (result["error"] or "")
    connector.upload_file.assert_not_called()


async def test_upload_kimi_log_upload_failure_propagates():
    remote = AsyncMock()
    remote.fetch_kimi_run_output = AsyncMock(return_value="STATUS: scoped\n")
    connector = AsyncMock()
    connector.upload_file = AsyncMock(
        return_value={"ok": False, "error": "http_503", "retryable": True, "data": None}
    )
    acts = AlertActivities(remote_script=remote, todoist_connector=connector)
    env = ActivityEnvironment()
    result = await env.run(acts.upload_kimi_log, "/tmp/k.out", "fp")
    assert result["ok"] is False
    assert result["error"] == "http_503"
    assert result["file_attachment"] is None


# ── record_verdict_to_kg (Pandora investigation → KG ingest) ──


async def test_record_verdict_to_kg_ingests_content_and_claims():
    """A succeeded investigation persists the transcript so the next
    investigation of the same resource can recall the diagnosis via
    search_knowledge / ask_knowledge."""
    kc = AsyncMock()
    kc.ingest_content = AsyncMock(return_value={"job_id": "job-1"})
    acts = AlertActivities(knowledge_connector=kc)
    env = ActivityEnvironment()
    alert = {
        "title": "OOM on aegis_worker",
        "fingerprint": "fp-oom-1",
        "source": "alertmanager",
    }
    verdict = {
        "status": "actionable",
        "root_cause": "Worker hit memory limit during kimi batch poll",
        "suggested_fix": "Raise mem_limit to 4GB and add OOM kill alert",
        "confidence": 0.82,
    }
    transcript = "Found three OOMKilled events in dmesg for aegis_worker.1\nSTATUS: investigated"

    result = await env.run(acts.record_verdict_to_kg, alert, verdict, transcript)

    assert result["ingested"] is True
    # ingest_content was called with the transcript (the searchable record)
    kc.ingest_content.assert_awaited_once()
    ic_kwargs = kc.ingest_content.await_args.kwargs
    assert ic_kwargs["source_type"] == "alert_investigation"
    assert "OOMKilled" in ic_kwargs["raw_text"]


async def test_record_verdict_to_kg_no_connector_returns_no_op():
    """No knowledge_connector → activity returns ingested=False cleanly,
    doesn't raise. Mirrors the gather_alert_knowledge / investigate
    no-connector paths."""
    acts = AlertActivities(knowledge_connector=None)
    env = ActivityEnvironment()
    result = await env.run(
        acts.record_verdict_to_kg,
        {"title": "x", "fingerprint": "fp", "source": "test"},
        {"status": "inconclusive", "confidence": 0.0},
        "",
    )
    assert result["ingested"] is False
    assert result["reason"] == "no_connector"


async def test_record_verdict_to_kg_swallows_errors():
    """Knowledge-service failures must NOT bubble up — the verdict was
    still computed; we just lose recall for this incident."""
    kc = AsyncMock()
    kc.ingest_content = AsyncMock(side_effect=RuntimeError("KS unreachable"))
    acts = AlertActivities(knowledge_connector=kc)
    env = ActivityEnvironment()
    result = await env.run(
        acts.record_verdict_to_kg,
        {"title": "x", "fingerprint": "fp", "source": "test"},
        {"status": "actionable", "root_cause": "rc", "confidence": 0.5},
        "some transcript",
    )
    assert result["ingested"] is False
    assert "KS unreachable" in result["reason"]


# ── score_resource_relevance (Gate-0 deny-by-default repo confirmation) ──
#
# The activity fetches all repository resources, runs the pure scorer
# (aegis_worker.relevance.score_resources) against the alert CONTENT, and
# returns {confident, resolved_resource_id, candidates}. A mock db_pool's
# .fetch returns row-like dicts (dict["id"]/["title"]/["metadata"] all work);
# metadata is passed as a plain dict so the activity's `if isinstance(m, str)`
# json.loads guard is bypassed.


def _resource_row(rid: str, title: str, path: str, github_repo: str) -> dict:
    """A fake asyncpg-row-like object — a plain dict supports row["id"] etc."""
    return {
        "id": rid,
        "title": title,
        "metadata": {"path": path, "github_repo": github_repo},
    }


async def test_score_resource_relevance_confident_sentry_service_match():
    """Sentry alert with service=bcp resolving to the bcp repo → confident:
    the independent-source exact service match scores bcp 1.0 and aegis 0.0,
    so bcp is the strict argmax."""
    pool = AsyncMock()
    pool.fetch = AsyncMock(
        return_value=[
            _resource_row("res-bcp", "BCP", "bcp", "acme/bcp"),
            _resource_row("res-aegis", "AEGIS", "aegis", "youruser/aegis"),
        ]
    )
    act = AlertActivities(db_pool=pool)
    env = ActivityEnvironment()
    alert = {
        "source": "sentry",
        "service": "bcp",
        "title": "KeyError",
        "description": "x",
    }
    result = await env.run(act.score_resource_relevance, alert, "res-bcp")

    assert result["confident"] is True
    assert result["resolved_resource_id"] == "res-bcp"
    # The query is the repository-only fetch.
    sql = pool.fetch.await_args.args[0]
    assert "kind = 'repository'" in sql


async def test_score_resource_relevance_not_confident_chat_wrong_pick():
    """A chat alert (non-independent source) whose content points at the
    trading pipeline but resolved to bcp → not confident. The returned
    candidates are a non-empty list of enriched dicts, and TSP appears."""
    pool = AsyncMock()
    pool.fetch = AsyncMock(
        return_value=[
            _resource_row("res-bcp", "BCP", "bcp", "acme/bcp"),
            _resource_row(
                "res-tsp",
                "Trading System Pipeline",
                "trading-system-pipeline",
                "youruser/trading-system-pipeline",
            ),
        ]
    )
    act = AlertActivities(db_pool=pool)
    env = ActivityEnvironment()
    alert = {
        "source": "todoist-chat",
        "service": "bcp",
        "title": "equities pipeline timeout",
        "description": "equities_fundamentals_pipeline step timed out",
    }
    result = await env.run(act.score_resource_relevance, alert, "res-bcp")

    assert result["confident"] is False
    candidates = result["candidates"]
    assert isinstance(candidates, list) and candidates
    expected_keys = {
        "resource_id",
        "resource_title",
        "resource_path",
        "github_repo",
        "label",
        "score",
    }
    for c in candidates:
        assert expected_keys <= set(c.keys())
    assert "res-tsp" in {c["resource_id"] for c in candidates}
    tsp = next(c for c in candidates if c["resource_id"] == "res-tsp")
    assert tsp["resource_path"] == "trading-system-pipeline"
    assert tsp["github_repo"] == "youruser/trading-system-pipeline"


async def test_score_resource_relevance_fail_open_no_pool():
    """No db_pool → fail-open: confident=True, empty candidates, no crash."""
    act = AlertActivities(db_pool=None)
    env = ActivityEnvironment()
    alert = {"source": "sentry", "service": "bcp", "title": "x", "description": ""}
    result = await env.run(act.score_resource_relevance, alert, "res-bcp")
    assert result["confident"] is True
    assert result["resolved_resource_id"] == "res-bcp"
    assert result["candidates"] == []


async def test_score_resource_relevance_fail_open_on_fetch_error():
    """A db_pool whose .fetch raises → fail-open confident=True, no candidates."""
    pool = AsyncMock()
    pool.fetch = AsyncMock(side_effect=RuntimeError("db down"))
    act = AlertActivities(db_pool=pool)
    env = ActivityEnvironment()
    alert = {"source": "sentry", "service": "bcp", "title": "x", "description": ""}
    result = await env.run(act.score_resource_relevance, alert, "res-bcp")
    assert result["confident"] is True
    assert result["candidates"] == []


# ── run_investigation worktree cleanup ────────────────────────────────


async def test_run_investigation_removes_worktree_on_success(mock_db_pool, mock_remote_script):
    """run_investigation reads worktree_path from start_kimi_run's return and
    removes the per-run worktree in its finally block after a successful run."""
    mock_remote_script.start_kimi_run = AsyncMock(
        return_value={
            "status": "running",
            "run_id": "run123",
            "repo": "bcp",
            "repo_path": "/home/user/repos/bcp",
            "output_file": "/tmp/bcp-kimi-run-run123.jsonl",
            "worktree_path": "/base/bcp-aegis-wt/run123",
        }
    )
    mock_remote_script.remove_worktree = AsyncMock()
    output_jsonl = (
        '{"session_id": "sess-abc"}\nBRANCH: aegis:aegis-fix/fp123\n'
        "Kimi output here\nSTATUS: investigated\n"
    )
    mock_remote_script.fetch_kimi_run_output = AsyncMock(return_value=output_jsonl)

    act = AlertActivities(
        db_pool=mock_db_pool,
        remote_script=mock_remote_script,
        kimi_binary="/usr/local/bin/kimi",
    )
    env = ActivityEnvironment()
    alert = {
        "title": "OOM",
        "severity": "critical",
        "fingerprint": "fp123",
        "source": "alertmanager",
        "description": "",
    }
    result = await env.run(act.run_investigation, alert, SAMPLE_RESOURCES[:1], "")

    assert result["status"] == "succeeded"
    mock_remote_script.remove_worktree.assert_awaited_once_with(
        "/base/bcp-aegis-wt/run123", host=""
    )


# --- build_alert_signature: alertmanager dedup signature ---------------------


def test_build_alert_signature_alertmanager_stable_across_fingerprints():
    """An alertmanager alert with service+alertname gets a stable signature,
    and a SAME logical alert with a DIFFERENT fingerprint collapses onto it."""
    alert_a = {
        "source": "alertmanager",
        "service": "equities_fundamentals_pipeline",
        "fingerprint": "fp-aaa",
        "labels": {"alertname": "DagsterRunFailed"},
    }
    alert_b = {
        "source": "alertmanager",
        "service": "equities_fundamentals_pipeline",
        "fingerprint": "fp-bbb",  # different fingerprint, same logical alert
        "labels": {"alertname": "DagsterRunFailed"},
    }
    sig_a = build_alert_signature(alert_a)
    sig_b = build_alert_signature(alert_b)
    assert sig_a.startswith("alertmanager-class:")
    assert sig_a != ""
    assert sig_a == sig_b


def test_build_alert_signature_alertmanager_falls_back_to_title_slug():
    """When no alertname label is present, the signature is derived from a
    slugified title (still stable, non-empty)."""
    alert = {
        "source": "prometheus",
        "service": "node-a",
        "title": "Disk usage above 90% on /var partition!!!",
    }
    sig = build_alert_signature(alert)
    assert sig.startswith("prometheus-class:node-a:")
    assert sig != ""
    # slugified: lowercased, no punctuation runs
    assert "!!!" not in sig
    assert sig == sig.lower()


def test_build_alert_signature_sentry_unchanged():
    """Sentry alerts still produce their sentry-class signature."""
    alert = {
        "source": "sentry",
        "service": "bcp",
        "raw_payload": {"metadata": {"type": "IncompatiblePeer"}},
    }
    assert build_alert_signature(alert) == "sentry-class:bcp:IncompatiblePeer"


def test_build_alert_signature_empty_when_nothing_stable():
    """No service AND no alertname AND no title → empty signature."""
    alert = {"source": "alertmanager", "fingerprint": "fp-xyz", "labels": {}}
    assert build_alert_signature(alert) == ""


# --- assess_investigation: nested-JSON unwrap + inconclusive guard -----------


async def test_assess_investigation_unwraps_nested_json_root_cause(mock_db_pool):
    """When the LLM stuffs a JSON object (as a string) into root_cause, the
    verdict unwraps the inner fields instead of posting raw JSON."""
    nested = json.dumps(
        {
            "status": "actionable",
            "root_cause": "The error is a missing DB index on equities.fundamentals.",
            "suggested_fix": "Add a btree index on (security_id, period).",
            "confidence": 0.8,
        }
    )
    llm = AsyncMock()
    llm.think.return_value = {
        "response": json.dumps(
            {
                "status": "actionable",
                "root_cause": nested,
                "suggested_fix": "",
                "confidence": 0.0,
            }
        ),
        "model": "qwen3:14b",
        "prompt_tokens": 10,
        "completion_tokens": 5,
    }
    activities = AlertActivities(db_pool=mock_db_pool, llm_client=llm)
    env = ActivityEnvironment()
    verdict = await env.run(
        activities.assess_investigation,
        {"title": "Pipeline failed", "severity": "critical"},
        "some investigation output",
    )
    assert set(verdict.keys()) == {"status", "root_cause", "suggested_fix", "confidence"}
    assert verdict["root_cause"] == "The error is a missing DB index on equities.fundamentals."
    assert verdict["suggested_fix"] == "Add a btree index on (security_id, period)."
    assert verdict["status"] == "actionable"
    assert verdict["confidence"] == 0.8


async def test_assess_investigation_empty_verdict_becomes_inconclusive(mock_db_pool):
    """confidence 0 + empty fix + empty root_cause must not be presented as
    actionable — it becomes inconclusive."""
    llm = AsyncMock()
    llm.think.return_value = {
        "response": json.dumps(
            {
                "status": "actionable",
                "root_cause": "",
                "suggested_fix": "",
                "confidence": 0.0,
            }
        ),
        "model": "qwen3:14b",
        "prompt_tokens": 10,
        "completion_tokens": 5,
    }
    activities = AlertActivities(db_pool=mock_db_pool, llm_client=llm)
    env = ActivityEnvironment()
    verdict = await env.run(
        activities.assess_investigation,
        {"title": "Vague alert", "severity": "warning"},
        "no concrete observations",
    )
    assert verdict["status"] == "inconclusive"
    assert verdict["root_cause"] == ""
    assert verdict["suggested_fix"] == ""
    assert verdict["confidence"] == 0.0


async def test_run_investigation_threads_github_repo_to_launcher(
    mock_db_pool, mock_remote_script
):
    """start_kimi_run must receive the primary resource's github_repo so the
    connector can route org repos to the claude engine on the base host."""
    output_jsonl = '{"session_id": "s1"}\nSTATUS: investigated\n'
    mock_remote_script.fetch_kimi_run_output = AsyncMock(return_value=output_jsonl)
    act = AlertActivities(
        db_pool=mock_db_pool,
        remote_script=mock_remote_script,
        kimi_binary="/usr/local/bin/kimi",
    )
    env = ActivityEnvironment()
    alert = {
        "title": "Bug",
        "severity": "warning",
        "fingerprint": "fp9",
        "source": "todoist-chat",
        "description": "",
    }
    resources = [
        {
            "resource_id": "res-bcp",
            "resource_title": "BCP",
            "resource_path": "acme/bcp",
            "github_repo": "Acme/bcp",
            "confidence": 0.9,
        }
    ]
    result = await env.run(act.run_investigation, alert, resources, "")
    assert result["status"] == "succeeded"
    args, kwargs = mock_remote_script.start_kimi_run.call_args
    assert args[0] == "acme/bcp"
    assert kwargs["github_repo"] == "Acme/bcp"
    # JIT clone removed — the fixed checkout is the only source.
    assert "clone_url" not in kwargs
