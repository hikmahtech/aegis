"""Tests for the `aegis_self_diagnose` chat tool (pandora self-healing)."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis.config import Settings
from aegis.services.chat import (
    _AEGIS_SELF_DIAGNOSE_MAX_WAIT,
    _TOOL_TIMEOUT_OVERRIDES,
    AGENT_TOOL_SETS,
    CHAT_TOOLS,
    TOOL_EXECUTORS,
    ToolContext,
    _build_aegis_self_diagnose_prompt,
    _exec_aegis_self_diagnose,
    _slugify_issue,
    send_message,
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
async def test_exec_returns_within_budget_when_fetch_hangs(monkeypatch):
    """Regression for the 3/3 prod timeouts (agent=pandoras-actor, 2026-07-15).

    A hung SSH `cat` in the poll loop must NOT block the executor past its
    deadline — the per-fetch hard timeout degrades the hang to a skipped poll,
    the deadline bounds the loop, and the tool still returns `still_running`
    (preserving run_id/output_file) well inside its budget rather than being
    guillotined by the outer tool-timeout (which loses the run_id).
    """
    import aegis.services.chat as chat_mod

    monkeypatch.setattr(chat_mod, "_AEGIS_SELF_DIAGNOSE_MAX_WAIT", 0.5)
    monkeypatch.setattr(chat_mod, "_AEGIS_SELF_DIAGNOSE_POLL", 0.1)
    monkeypatch.setattr(chat_mod, "_AEGIS_SELF_DIAGNOSE_FETCH_TIMEOUT", 0.15)

    mock_connector = MagicMock()
    mock_connector.start_kimi_run = AsyncMock(
        return_value={
            "status": "running",
            "run_id": "run-hang",
            "output_file": "/tmp/aegis-kimi-run-run-hang.jsonl",
        }
    )

    # Every fetch hangs far longer than the whole budget — the old unbounded
    # `await fetch(...)` would have blocked here until the outer guillotine.
    async def _hanging_fetch(*_args, **_kwargs):
        await asyncio.sleep(30)
        return "never reached\nSTATUS: investigated\n"

    mock_connector.fetch_kimi_run_output = _hanging_fetch
    ctx = ToolContext(settings=_settings(), remote_script_connector=mock_connector)

    started = time.monotonic()
    out = json.loads(
        await _exec_aegis_self_diagnose(
            MagicMock(), {"issue": "anything", "mode": "investigate"}, ctx
        )
    )
    elapsed = time.monotonic() - started

    # Returned gracefully instead of hanging on the 30s fetch or timing out.
    assert out["status"] == "still_running"
    assert out["run_id"] == "run-hang"
    assert out["output_file"] == "/tmp/aegis-kimi-run-run-hang.jsonl"
    # No output was ever collected (fetch always preempted) → placeholder note.
    assert "no output yet" in out["transcript"]
    # Hard bound: MAX_WAIT (0.5s) + one preempted fetch (0.15s) + slack — nowhere
    # near the 30s hang. A regression (unbounded fetch) would blow this.
    assert elapsed < 5.0


# =====================================================================
# The 30,000 ms cap itself (#140) — the tool loop's timeout wiring
# =====================================================================


def _loop_settings():
    return Settings(
        database_url="postgresql://test:test@localhost/test",
        litellm_url="https://litellm.test/v1",
        temporal_ui_url="https://temporal.test",
        n8n_ui_url="https://n8n.test",
        admin_username="admin",
        admin_password="admin",
        n8n_webhook_secret="test-secret",
        model_balanced="test-model",
        tool_calling_enabled=True,
        tool_max_iterations=5,
        tool_result_max_bytes=4096,
        tool_timeout_seconds=30,
    )


def _agent_pool():
    """AsyncMock pool that answers every agent lookup as pandoras-actor."""
    pool = AsyncMock()
    pool.fetchrow.return_value = {
        "id": "pandoras-actor",
        "name": "Pandora",
        "system_prompt_path": "personalities/pandoras-actor/SOUL.md",
    }
    pool.fetch.return_value = []
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    return pool


@pytest.mark.asyncio
async def test_tool_loop_applies_the_self_diagnose_timeout_override(monkeypatch):
    """Regression for the ONLY failure this tool has ever had in production.

    All 3 lifetime invocations (pandoras-actor, 2026-07-15 21:28-21:29 IST)
    died identically: `latency_ms=30000`, `Tool 'aegis_self_diagnose' timed out
    after 30s`. Nothing in the executor caused that — the outer tool loop's
    `asyncio.wait_for` was simply set to `settings.tool_timeout_seconds`, and a
    tool that polls a remote coding CLI for 8 minutes can never fit in 30s.
    PR #73 added `_TOOL_TIMEOUT_OVERRIDES`, but nothing ever pinned that the
    loop *consults* it, so a refactor could quietly restore the 30s guillotine.

    Proven two ways that cannot mask each other, both against the SAME slow
    executor and the SAME 0.05s default:
      * `aegis_self_diagnose` survives          -> the override is applied;
      * `list_nodes` (no override) still dies   -> the guillotine is armed,
        so the first result isn't just "timeouts are broken everywhere".
    """
    import aegis.services.chat as chat_mod

    async def _slow(_pool, _args, _ctx):
        await asyncio.sleep(0.30)
        return json.dumps({"ok": "executor finished"})

    monkeypatch.setitem(chat_mod.TOOL_EXECUTORS, "aegis_self_diagnose", _slow)
    monkeypatch.setitem(chat_mod.TOOL_EXECUTORS, "list_nodes", _slow)

    settings = _loop_settings()
    # Assigned, not constructed: the field is typed int, so passing 0.05 to
    # Settings(...) would coerce it to 0 and every tool would time out.
    settings.tool_timeout_seconds = 0.05

    turns = [
        {
            "response": "",
            "tool_calls": [
                {
                    "id": "tc-diag",
                    "name": "aegis_self_diagnose",
                    "arguments": json.dumps({"issue": "why is X slow", "mode": "investigate"}),
                },
                {
                    "id": "tc-nodes",
                    "name": "list_nodes",
                    "arguments": json.dumps({"context": "swarm"}),
                },
            ],
            "model": "test-model",
            "prompt_tokens": 10,
            "completion_tokens": 5,
        },
        {
            "response": "Done.",
            "tool_calls": [],
            "model": "test-model",
            "prompt_tokens": 20,
            "completion_tokens": 10,
        },
    ]
    seen: list[list[dict]] = []

    async def _chat(messages, model, tools=None):
        seen.append([dict(m) for m in messages])
        return turns.pop(0)

    llm = AsyncMock()
    llm.chat = _chat

    await send_message(
        _agent_pool(), llm, "pandoras-actor", "diagnose yourself", settings=settings
    )

    # Messages the model saw on its second turn = the tool results.
    by_id = {
        m["tool_call_id"]: m["content"] for m in seen[-1] if m.get("role") == "tool"
    }
    # Assert the BODY, not merely "no error": the executor's own payload has to
    # be what came back, which only happens if it ran to completion.
    assert json.loads(by_id["tc-diag"]) == {"ok": "executor finished"}
    # Control: identical executor, no override -> guillotined at 0.05s.
    assert "timed out after 0.05s" in by_id["tc-nodes"]

    # And the configured budget must clear the poll loop it wraps, with room
    # for the launch round-trip. 30 is a literal here on purpose — it is the
    # production default that produced all three 30,000 ms failures.
    assert _TOOL_TIMEOUT_OVERRIDES["aegis_self_diagnose"] > 30
    assert _TOOL_TIMEOUT_OVERRIDES["aegis_self_diagnose"] > _AEGIS_SELF_DIAGNOSE_MAX_WAIT


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
