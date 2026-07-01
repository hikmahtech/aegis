"""Tests for the `aegis_self_diagnose` chat tool (pandora self-healing)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis.services.chat import (
    AGENT_TOOL_SETS,
    CHAT_TOOLS,
    TOOL_EXECUTORS,
    ToolContext,
    _build_aegis_self_diagnose_prompt,
    _exec_aegis_self_diagnose,
    _slugify_issue,
)


def _settings(**overrides):
    base = {
        "aegis_self_repo_path": "personal/aegis",
        "kimi_cli_binary_path": "/home/user/.local/bin/kimi",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_tool_registered_and_granted_to_pandora():
    """The tool is in CHAT_TOOLS, TOOL_EXECUTORS, and pandora's AGENT_TOOL_SETS."""
    names = {t["function"]["name"] for t in CHAT_TOOLS}
    assert "aegis_self_diagnose" in names
    assert "aegis_self_diagnose" in TOOL_EXECUTORS
    assert "aegis_self_diagnose" in AGENT_TOOL_SETS["pandoras-actor"]
    # Other agents must NOT see it — it's scoped to pandora because the
    # branch+PR convention assumes pandora's smart-tier reasoning.
    assert "aegis_self_diagnose" not in AGENT_TOOL_SETS["sebas"]
    assert "aegis_self_diagnose" not in AGENT_TOOL_SETS["raphael"]
    assert "aegis_self_diagnose" not in AGENT_TOOL_SETS.get("maou", set())


def test_slugify_issue():
    assert _slugify_issue("Renewal radar spam") == "renewal-radar-spam"
    # Long inputs are capped at 32 chars; trailing hyphens stripped.
    long = _slugify_issue("a" * 50)
    assert len(long) <= 32
    # Empty / unicode-garbage inputs fall back to a stable token.
    assert _slugify_issue("") == "issue"
    assert _slugify_issue("!!!") == "issue"


def test_build_prompt_investigate_mode_does_not_request_commit():
    prompt = _build_aegis_self_diagnose_prompt(
        issue="why does X fail?", mode="investigate", fix_branch="aegis-fix/x"
    )
    assert "Mode: investigate" in prompt
    assert "Do NOT modify files" in prompt
    # Branch convention text is omitted in investigate mode.
    assert "Create branch" not in prompt
    # STATUS footer is REQUIRED — polling loop depends on it.
    assert "STATUS:" in prompt
    assert "STATUS: investigated" in prompt
    assert "STATUS: proposed" in prompt


def test_build_prompt_fix_mode_requests_branch_and_pr():
    prompt = _build_aegis_self_diagnose_prompt(
        issue="rename foo to bar", mode="fix", fix_branch="aegis-fix/rename-foo-bar"
    )
    assert "Mode: fix" in prompt
    assert "Create branch `aegis-fix/rename-foo-bar`" in prompt
    assert "gh pr create --draft" in prompt
    assert "Do NOT commit directly to main" in prompt
    # Both modes still require the STATUS footer.
    assert "STATUS: shipped" in prompt


@pytest.mark.asyncio
async def test_exec_rejects_missing_issue():
    ctx = ToolContext(settings=_settings(), remote_script_connector=MagicMock())
    out = json.loads(await _exec_aegis_self_diagnose(MagicMock(), {"mode": "investigate"}, ctx))
    assert "error" in out
    assert "issue is required" in out["error"]


@pytest.mark.asyncio
async def test_exec_rejects_bad_mode():
    ctx = ToolContext(settings=_settings(), remote_script_connector=MagicMock())
    out = json.loads(
        await _exec_aegis_self_diagnose(MagicMock(), {"issue": "x", "mode": "yolo"}, ctx)
    )
    assert "error" in out
    assert "investigate" in out["error"]


@pytest.mark.asyncio
async def test_exec_rejects_missing_remote_script_connector():
    ctx = ToolContext(settings=_settings(), remote_script_connector=None)
    out = json.loads(
        await _exec_aegis_self_diagnose(
            MagicMock(), {"issue": "x", "mode": "investigate"}, ctx
        )
    )
    assert "error" in out
    assert "connector" in out["error"]


@pytest.mark.asyncio
async def test_exec_returns_completed_when_kimi_emits_status_footer():
    """Happy path: start_kimi_run → fetch returns text ending in STATUS footer
    → tool returns status='completed' with transcript."""
    mock_connector = MagicMock()
    mock_connector.start_kimi_run = AsyncMock(
        return_value={
            "status": "running",
            "run_id": "run-xyz",
            "output_file": "/tmp/aegis-kimi-run-run-xyz.jsonl",
            "repo_path": "/home/user/aegis",
        }
    )
    transcript = "Investigated. Found the bug at clarify.py:123.\nSTATUS: investigated\n"
    mock_connector.fetch_kimi_run_output = AsyncMock(return_value=transcript)
    ctx = ToolContext(settings=_settings(), remote_script_connector=mock_connector)

    out = json.loads(
        await _exec_aegis_self_diagnose(
            MagicMock(),
            {"issue": "explain ClarifyFlow watermark", "mode": "investigate"},
            ctx,
        )
    )

    assert out["status"] == "completed"
    assert out["run_id"] == "run-xyz"
    assert "STATUS: investigated" in out["transcript"]
    # investigate mode → no fix_branch promised back to the user.
    assert out["fix_branch"] is None

    # Verify the connector was called with the configured self-repo path —
    # a fixed workspace checkout, no clone URL (JIT clone removed).
    call = mock_connector.start_kimi_run.call_args
    assert call.args[0] == "personal/aegis"  # repo
    assert "ClarifyFlow watermark" in call.args[1]  # prompt contains the issue
    assert "Do NOT modify files" in call.args[1]  # investigate mode
    assert call.kwargs["kimi_binary"] == "/home/user/.local/bin/kimi"
    assert "clone_url" not in call.kwargs


@pytest.mark.asyncio
async def test_exec_returns_still_running_on_polling_timeout(monkeypatch):
    """If kimi never emits STATUS within the max-wait window, the tool returns
    `still_running` with whatever transcript is available."""
    # Speed up the polling loop so the test doesn't sit for 8 minutes.
    import aegis.services.chat as chat_mod

    monkeypatch.setattr(chat_mod, "_AEGIS_SELF_DIAGNOSE_MAX_WAIT", 1.0)
    monkeypatch.setattr(chat_mod, "_AEGIS_SELF_DIAGNOSE_POLL", 0.2)

    mock_connector = MagicMock()
    mock_connector.start_kimi_run = AsyncMock(
        return_value={
            "status": "running",
            "run_id": "run-stuck",
            "output_file": "/tmp/aegis-kimi-run-run-stuck.jsonl",
        }
    )
    # Output present but no STATUS footer → polling loop times out.
    mock_connector.fetch_kimi_run_output = AsyncMock(return_value="still thinking...\n")
    ctx = ToolContext(settings=_settings(), remote_script_connector=mock_connector)

    out = json.loads(
        await _exec_aegis_self_diagnose(
            MagicMock(), {"issue": "anything", "mode": "fix"}, ctx
        )
    )

    assert out["status"] == "still_running"
    assert out["run_id"] == "run-stuck"
    assert "still thinking" in out["transcript"]
    # fix mode promises a branch slug back to the user regardless of completion.
    assert out["fix_branch"] is not None
    assert out["fix_branch"].startswith("aegis-fix/")


@pytest.mark.asyncio
async def test_exec_propagates_kimi_launch_failure():
    mock_connector = MagicMock()
    mock_connector.start_kimi_run = AsyncMock(
        return_value={"status": "failed", "error": "ssh permission denied"}
    )
    ctx = ToolContext(settings=_settings(), remote_script_connector=mock_connector)

    out = json.loads(
        await _exec_aegis_self_diagnose(
            MagicMock(), {"issue": "x", "mode": "investigate"}, ctx
        )
    )
    assert "error" in out
    assert "ssh permission denied" in out["error"]
