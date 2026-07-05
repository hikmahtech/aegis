"""Gate-0 (Step 4.4) flow tests for AlertInvestigationFlow.

Gate-0 sits between resolve (Step 4) and the start-comment (Step 4.5). It
calls `score_resource_relevance`; when the resolved repo is NOT confidently
relevant it either:
  • spawns a REAL InteractionFlow child (kind="choice") to let the user pick
    the right repo, then rebuilds resources_list to the picked repo, OR
  • aborts with status="repo_unconfirmed" (no kimi) when there are no
    candidates to offer / the user declines / the interaction archives.

These tests drive the not-confident path with a real InteractionFlow child:
  • pick-path: signal the child with the chosen resource_id → run_investigation
    runs against the picked repo (best-effort signal test).
  • abort-path (deterministic): score returns confident=False + EMPTY
    candidates → the flow aborts WITHOUT spawning InteractionFlow →
    status="repo_unconfirmed" and run_investigation is never called.

Setup mirrors tests/worker/test_alert_flow_v2.py: a WorkflowEnvironment
with stub activities + the real InteractionFlow registered alongside
AlertInvestigationFlow. The signal mechanism copies
tests/worker/test_interaction_flow.py (deterministic child id + submit_response).
"""

from __future__ import annotations

import asyncio
import re
from uuid import uuid4

from aegis_worker.activities.interactions import (
    ApplyTimeoutInput,
    InsertInteractionInput,
    InsertInteractionResult,
    ResolveInteractionInput,
    ResolveInteractionResult,
)
from aegis_worker.flows.alert_investigation import (
    AlertInvestigationFlow,
    _build_repo_confirm_prompt,
)
from aegis_worker.flows.interaction import InteractionFlow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

# ---------------------------------------------------------------------------
# Mutable test state
# ---------------------------------------------------------------------------

_state: dict = {}

# Two candidates Gate-0 offers when not confident. TSP first, BCP second.
_TSP_CANDIDATE = {
    "resource_id": "RID_TSP",
    "resource_title": "Trading System Pipeline",
    "resource_path": "trading-system-pipeline",
    "github_repo": "youruser/trading-system-pipeline",
    "label": "youruser/trading-system-pipeline",
    "score": 0.5,
}
_BCP_CANDIDATE = {
    "resource_id": "RID_BCP",
    "resource_title": "BCP",
    "resource_path": "bcp",
    "github_repo": "acme/bcp",
    "label": "acme/bcp",
    "score": 0.0,
}


def _reset(**overrides):
    _state.clear()
    _state.update(
        {
            "score_result": {
                "confident": False,
                "resolved_resource_id": "RID_BCP",
                "candidates": [_TSP_CANDIDATE, _BCP_CANDIDATE],
            },
            "run_investigation_result": {
                "status": "succeeded",
                "output": "Root cause found. STATUS: investigated",
                "session_id": "sess-1",
                "branch": "",
                "branches": {},
            },
            "assess_result": {
                "status": "actionable",
                "root_cause": "rc",
                "suggested_fix": "fix",
                "confidence": 0.8,
            },
            "run_investigation_called": False,
            "run_investigation_resources": None,
            "posted_notes": [],
        }
    )
    _state.update(overrides)


# ---------------------------------------------------------------------------
# Stub activities — resolve always returns a single (bcp) repo so Gate-0 runs.
# ---------------------------------------------------------------------------


@activity.defn(name="check_dedup")
async def stub_check_dedup(fingerprint: str, hours: int) -> dict:
    return {"is_duplicate": False}


@activity.defn(name="get_verification_delay")
async def stub_get_verification_delay(alert: dict) -> dict:
    return {"delay_seconds": 0, "reason": "immediate"}


@activity.defn(name="check_alert_resolved")
async def stub_check_alert_resolved(fingerprint: str, window_minutes: int) -> dict:
    return {"resolved": False}


@activity.defn(name="resolve_alert_resource")
async def stub_resolve_alert_resource(alert: dict) -> dict:
    return {
        "resource_id": "RID_BCP",
        "resource_title": "BCP",
        "resource_path": "bcp",
        "github_repo": "acme/bcp",
        "confidence": 0.9,
        "source": "service_match",
        "resources": [
            {
                "resource_id": "RID_BCP",
                "resource_title": "BCP",
                "resource_path": "bcp",
                "github_repo": "acme/bcp",
                "confidence": 0.9,
            }
        ],
    }


@activity.defn(name="score_resource_relevance")
async def stub_score_resource_relevance(alert: dict, resolved_resource_id: str) -> dict:
    return _state["score_result"]


@activity.defn(name="reresolve_with_hint")
async def stub_reresolve_with_hint(alert: dict, hint: str) -> dict:
    return {"confident": False, "candidates": []}


@activity.defn(name="check_active_work")
async def stub_check_active_work(alert: dict, repo: str) -> dict:
    _state["check_active_work_called"] = True
    return {"active": False, "reasons": []}


@activity.defn(name="gather_alert_knowledge")
async def stub_gather_alert_knowledge(title: str, project: str, alert_name: str = "") -> str:
    return ""


@activity.defn(name="run_investigation")
async def stub_run_investigation(alert: dict, resources: list[dict], runbook: str, *_a) -> dict:
    _state["run_investigation_called"] = True
    _state["run_investigation_resources"] = resources
    return _state["run_investigation_result"]


@activity.defn(name="investigate")
async def stub_investigate(alert: dict, system_prompt: str) -> dict:
    return {"investigation": "", "actionable": True, "auto_fixable": False}


@activity.defn(name="assess_investigation")
async def stub_assess_investigation(alert: dict, investigation_output: str) -> dict:
    return _state["assess_result"]


@activity.defn(name="record_verdict_to_kg")
async def stub_record_verdict_to_kg(alert: dict, verdict: dict, output: str) -> dict:
    return {"ingested": False}


@activity.defn(name="log_alert")
async def stub_log_alert(alert: dict) -> None:
    pass


@activity.defn(name="send_system_event")
async def stub_send_system_event(msg: str) -> None:
    pass


@activity.defn(name="send_message")
async def stub_send_message(
    agent_id: str, msg: str, chat_id: int, reply_markup: dict | None = None
) -> None:
    pass


@activity.defn(name="check_alert_mute")
async def stub_check_alert_mute(_inp) -> bool:
    return False


@activity.defn(name="write_alert_mute")
async def stub_write_alert_mute(_inp) -> None:
    pass


@activity.defn(name="accumulate_digest_item")
async def stub_accumulate_digest_item(payload: dict) -> None:
    pass


@activity.defn(name="capture_to_inbox")
async def stub_capture_to_inbox(
    source_tag: str,
    external_id: str,
    title: str,
    description: str | None = None,
    extra_labels: list[str] | None = None,
) -> str | None:
    return "real-captured-1"


@activity.defn(name="post_task_note")
async def stub_post_task_note(
    task_id: str,
    content: str,
    file_attachment: dict | None = None,
    workflow_id: str = "",
    run_id: str = "",
) -> dict:
    _state.setdefault("posted_notes", []).append({"task_id": task_id, "content": content})
    return {"ok": True, "error": None}


@activity.defn(name="upload_kimi_log")
async def stub_upload_kimi_log(output_file: str, filename_hint: str, host: str = "") -> dict:
    return {"ok": False, "file_attachment": None, "file_name": "", "error": "skip"}


# ── InteractionFlow activity stubs (in-memory; the real flow handles signals) ─


@activity.defn(name="insert_interaction")
async def stub_insert_interaction(inp: InsertInteractionInput) -> InsertInteractionResult:
    return InsertInteractionResult(interaction_id="ia-gate0-test")


@activity.defn(name="send_interaction_card")
async def stub_send_card(
    interaction_id: str,
    agent_id: str,
    kind: str,
    prompt: str,
    options,
    allow_hint: bool = False,
) -> dict:
    _state["card_options"] = options
    _state.setdefault("card_prompts", []).append(prompt)
    return {"ok": True, "message_id": 1}


@activity.defn(name="update_interaction_message_id")
async def stub_update_msg(interaction_id: str, telegram_message_id: int) -> None:
    pass


@activity.defn(name="resolve_interaction")
async def stub_resolve_interaction(inp: ResolveInteractionInput) -> ResolveInteractionResult:
    return ResolveInteractionResult(already_resolved=False)


@activity.defn(name="apply_interaction_timeout")
async def stub_apply_timeout(inp: ApplyTimeoutInput) -> None:
    return None


@activity.defn(name="resolve_agents")
async def stub_resolve_agents(tags):
    # Seed mapping — infra → pandoras-actor (behavior unchanged).
    seed = {"finance": "maou", "infra": "pandoras-actor", "gtd": "sebas", "research": "raphael"}
    return {t: seed.get(t) for t in tags}


ALL_ACTIVITIES = [
    stub_resolve_agents,
    stub_check_alert_mute,
    stub_write_alert_mute,
    stub_check_dedup,
    stub_get_verification_delay,
    stub_check_alert_resolved,
    stub_resolve_alert_resource,
    stub_score_resource_relevance,
    stub_reresolve_with_hint,
    stub_check_active_work,
    stub_gather_alert_knowledge,
    stub_run_investigation,
    stub_investigate,
    stub_assess_investigation,
    stub_record_verdict_to_kg,
    stub_log_alert,
    stub_send_system_event,
    stub_send_message,
    stub_accumulate_digest_item,
    stub_capture_to_inbox,
    stub_post_task_note,
    stub_upload_kimi_log,
    stub_insert_interaction,
    stub_send_card,
    stub_update_msg,
    stub_resolve_interaction,
    stub_apply_timeout,
]


def _safe(text: str, max_len: int = 60) -> str:
    """Mirror the flow's _safe_workflow_id_segment for child-id derivation."""
    return re.sub(r"[^a-zA-Z0-9._\-]", "-", text)[:max_len]


def _make_alert(**overrides) -> dict:
    base = {
        "title": "equities pipeline timeout",
        "fingerprint": "fp-gate0-1",
        "severity": "error",
        "source": "todoist-chat",
        "service": "bcp",
        "description": "equities_fundamentals_pipeline step timed out",
        "labels": {},
        "raw_payload": {},
        "todoist_task_id": "TRACK_TASK_1",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_gate0_user_picks_repo_runs_investigation_on_pick():
    """not-confident → InteractionFlow child spawned → user signals RID_TSP →
    flow rebuilds resources_list to the TSP repo and run_investigation runs
    against it (best-effort signal test)."""
    _reset()
    parent_id = f"alert-gate0-pick-{uuid4().hex[:8]}"
    child_id = f"repo-confirm-{_safe('fp-gate0-1')}-{parent_id}"

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="test-q-gate0",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_ACTIVITIES,
        ),
    ):
        handle = await env.client.start_workflow(
            AlertInvestigationFlow.run,
            _make_alert(),
            id=parent_id,
            task_queue="test-q-gate0",
        )

        # Wait for the InteractionFlow child to come up, then signal the pick.
        child = env.client.get_workflow_handle(child_id)
        signalled = False
        for _ in range(80):
            try:
                desc = await child.describe()
            except Exception:
                await asyncio.sleep(0.05)
                continue
            if desc.status is not None and desc.status.name == "RUNNING":
                # Pick by INDEX (TSP is candidates[0]) — keys are indices, not
                # resource UUIDs, keeping callback payloads compact.
                await child.signal(InteractionFlow.submit_response, {"value": "0"})
                signalled = True
                break
            await asyncio.sleep(0.05)
        assert signalled, "InteractionFlow child never reached RUNNING to be signalled"

        result = await handle.result()

    # Regression guard: the card's option keys must keep callback_data
    # (`interaction:{36-char-uuid}:{key}`) compact —
    # resource UUIDs as keys (85 bytes) silently fail with BUTTON_DATA_INVALID.
    opts = _state.get("card_options")
    assert opts, "Gate-0 card was never sent"
    for key in opts:
        cd_len = len(f"interaction:{'x' * 36}:{key}")
        assert cd_len <= 64, f"option key {key!r} -> callback_data {cd_len}B > 64"

    # The FIRST card is the Gate-0 repo-confirm card (a later Gate-2 card may
    # follow). It must carry enough context to pick a repo: the issue title AND
    # its description (not just a generic "which repo?" line).
    gate0_prompt = (_state.get("card_prompts") or [""])[0]
    assert "equities pipeline timeout" in gate0_prompt
    assert "equities_fundamentals_pipeline" in gate0_prompt

    # Flow proceeded into investigation on the picked repo.
    assert _state["run_investigation_called"] is True
    resources = _state["run_investigation_resources"]
    assert resources, "run_investigation got no resources"
    assert resources[0]["resource_path"] == "trading-system-pipeline"
    assert result["status"] != "repo_unconfirmed"


async def test_gate0_empty_candidates_aborts_without_interaction():
    """Deterministic abort: score returns confident=False with EMPTY
    candidates → the flow aborts WITHOUT spawning InteractionFlow →
    status='repo_unconfirmed' and run_investigation is never called."""
    _reset(
        score_result={
            "confident": False,
            "resolved_resource_id": "RID_BCP",
            "candidates": [],
        }
    )

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="test-q-gate0",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_ACTIVITIES,
        ),
    ):
        result = await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_alert(),
            id=f"alert-gate0-abort-{uuid4().hex[:8]}",
            task_queue="test-q-gate0",
        )

    assert result["status"] == "repo_unconfirmed"
    assert result["task_id"] == "TRACK_TASK_1"
    assert _state["run_investigation_called"] is False


# ── pure helper: repo-confirm card body ──────────────────────────────────────


def test_repo_confirm_prompt_includes_title_meta_description_and_task_link():
    body = _build_repo_confirm_prompt(
        title="Equities importer MEMORY error",
        source="todoist-jira",
        severity="error",
        service="acme",
        description="The screener step ran out of memory during the equities import.",
        task_id="6gmMCHqc328HprVM",
    )
    assert "Which repository is this about?" in body
    assert "Equities importer MEMORY error" in body
    # context line + description + deep-link give the user what they need.
    assert "error · todoist-jira · acme" in body
    assert "ran out of memory" in body
    assert "https://app.todoist.com/app/task/6gmMCHqc328HprVM" in body


def test_repo_confirm_prompt_omits_link_for_synthetic_task_and_empty_desc():
    body = _build_repo_confirm_prompt(
        title="x",
        source="todoist-chat",
        severity="",
        service="",
        description="",
        task_id="item-123",  # synthetic, not a real Todoist task
    )
    assert "Open the task on Todoist" not in body
    assert "todoist.com/app/task" not in body
    # No description block when there's nothing to show.
    assert body.count("\n\n") <= 1


def test_repo_confirm_prompt_escapes_user_html():
    body = _build_repo_confirm_prompt(
        title="<script>alert(1)</script>",
        source="todoist-chat",
        severity="",
        service="",
        description="a & b <b>bold</b>",
        task_id="",
    )
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert "a &amp; b" in body
