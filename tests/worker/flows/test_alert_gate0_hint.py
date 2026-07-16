"""Gate-0 bounded hint-loop flow tests for AlertInvestigationFlow.

When score_resource_relevance is NOT confident, the flow opens a bounded loop:
present the top candidates with a free-text hint affordance (allow_hint=True for
the first _MAX_HINT_ROUNDS rounds); if the operator replies `hint:<text>`,
re-run Gate-0 resolution via reresolve_with_hint and re-present; otherwise honour
the pick (an index) or cancel. A human pick rebuilds resources_list, sets
`_repo_from_human=True`, and — critically — BYPASSES the active-work guard
(an explicit "investigate anyway").

Tests (start_local so we can signal the child InteractionFlow at each round):
  • test_hint_loop_then_pick: round 0 → `hint:owner/repo` → reresolve_with_hint
    called → round 1 → pick index 0 → investigation runs against the picked
    repo, and check_active_work is NEVER called (guard bypassed on human pick).
  • test_allow_hint_false_once_max_rounds_reached: after _MAX_HINT_ROUNDS hints
    the card for the final round is constructed with allow_hint=False (asserted
    via the send_interaction_card stub which captures the flag per round).

Harness mirrors tests/worker/flows/test_alert_investigation_gates.py.
"""

from __future__ import annotations

import asyncio
import re
from uuid import uuid4

import pytest
from aegis_worker.activities.interactions import (
    ApplyTimeoutInput,
    InsertInteractionInput,
    InsertInteractionResult,
    ResolveInteractionInput,
    ResolveInteractionResult,
)
from aegis_worker.flows.alert_investigation import AlertInvestigationFlow
from aegis_worker.flows.interaction import InteractionFlow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

# ---------------------------------------------------------------------------
# Mutable test state
# ---------------------------------------------------------------------------

_state: dict = {}

_TSP_CANDIDATE = {
    "resource_id": "RID_TSP",
    "resource_title": "Trading System Pipeline",
    "resource_path": "trading-system-pipeline",
    "github_repo": "youruser/trading-system-pipeline",
    "label": "youruser/trading-system-pipeline",
    "score": 0.5,
}
# Synthetic candidate the (stubbed) reresolve_with_hint surfaces from the hint.
_HINT_CANDIDATE = {
    "resource_id": "owner/repo",
    "resource_title": "owner/repo",
    "resource_path": "",
    "github_repo": "owner/repo",
    "label": "owner/repo (from hint)",
    "score": 1.0,
}


def _reset(**overrides):
    _state.clear()
    _state.update(
        {
            # First Gate-0 score: not confident, one candidate.
            "score_result": {
                "confident": False,
                "resolved_resource_id": "RID_TSP",
                "candidates": [_TSP_CANDIDATE],
            },
            # reresolve_with_hint return: still not confident, hint candidate on top.
            "reresolve_result": {
                "confident": False,
                "candidates": [_HINT_CANDIDATE, _TSP_CANDIDATE],
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
            "check_active_work_called": False,
            "reresolve_calls": [],
            "card_allow_hints": [],  # allow_hint flag per send_interaction_card call
        }
    )
    _state.update(overrides)


# ---------------------------------------------------------------------------
# Stub activities
# ---------------------------------------------------------------------------


@activity.defn(name="check_dedup")
async def stub_check_dedup(fingerprint: str, hours: int) -> dict:
    return {"is_duplicate": False}


@activity.defn(name="find_open_task_for_signature")
async def stub_find_open_task_for_signature(signature: str) -> str | None:
    return None


@activity.defn(name="record_signature_new_task")
async def stub_record_signature_new_task(signature: str, task_id: str) -> None:
    return None


@activity.defn(name="get_verification_delay")
async def stub_get_verification_delay(alert: dict) -> dict:
    return {"delay_seconds": 0, "reason": "immediate"}


@activity.defn(name="check_alert_resolved")
async def stub_check_alert_resolved(fingerprint: str, window_minutes: int) -> dict:
    return {"resolved": False}


@activity.defn(name="resolve_alert_resource")
async def stub_resolve_alert_resource(alert: dict) -> dict:
    return {
        "resource_id": "RID_TSP",
        "resource_title": "Trading System Pipeline",
        "resource_path": "trading-system-pipeline",
        "github_repo": "youruser/trading-system-pipeline",
        "confidence": 0.9,
        "source": "service_match",
        "resources": [
            {
                "resource_id": "RID_TSP",
                "resource_title": "Trading System Pipeline",
                "resource_path": "trading-system-pipeline",
                "github_repo": "youruser/trading-system-pipeline",
                "confidence": 0.9,
            }
        ],
    }


@activity.defn(name="score_resource_relevance")
async def stub_score_resource_relevance(alert: dict, resolved_resource_id: str) -> dict:
    return _state["score_result"]


@activity.defn(name="reresolve_with_hint")
async def stub_reresolve_with_hint(alert: dict, hint: str) -> dict:
    _state.setdefault("reresolve_calls", []).append(hint)
    return _state["reresolve_result"]


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
) -> dict:
    return {"ok": True}


@activity.defn(name="check_alert_mute")
async def stub_check_alert_mute(_inp) -> bool:
    return False


@activity.defn(name="write_alert_mute")
async def stub_write_alert_mute(_inp) -> None:
    pass


@activity.defn(name="accumulate_digest_item")
async def stub_accumulate_digest_item(payload: dict) -> None:
    _state.setdefault("digest_items", []).append(payload)


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
    return {"ok": True, "error": None}


@activity.defn(name="upload_kimi_log")
async def stub_upload_kimi_log(output_file: str, filename_hint: str, host: str = "") -> dict:
    return {"ok": False, "file_attachment": None, "file_name": "", "error": "skip"}


# ── InteractionFlow activity stubs ───────────────────────────────────────────


@activity.defn(name="insert_interaction")
async def stub_insert_interaction(inp: InsertInteractionInput) -> InsertInteractionResult:
    _state.setdefault("insert_ia", []).append((inp.kind, inp.origin))
    return InsertInteractionResult(interaction_id="ia-hint-test")


@activity.defn(name="send_interaction_card")
async def stub_send_card(
    interaction_id: str,
    agent_id: str,
    kind: str,
    prompt: str,
    options,
    allow_hint: bool = False,
) -> dict:
    _state.setdefault("card_allow_hints", []).append(allow_hint)
    return {"ok": True, "message_id": 1}


@activity.defn(name="resolve_interaction")
async def stub_resolve_interaction(inp: ResolveInteractionInput) -> ResolveInteractionResult:
    return ResolveInteractionResult(already_resolved=False)


@activity.defn(name="apply_interaction_timeout")
async def stub_apply_timeout(inp: ApplyTimeoutInput) -> None:
    return None


@activity.defn(name="stage_pending_pr")
async def stub_stage_pending_pr(inp) -> str:
    return "pr-uuid-stub"


@activity.defn(name="create_github_pr")
async def stub_create_github_pr(inp) -> dict:
    return {"pr_url": "", "status": "opened", "error": ""}


@activity.defn(name="resolve_agents")
async def stub_resolve_agents(tags):
    # Seed mapping — infra → pandoras-actor (behavior unchanged).
    seed = {"finance": "maou", "infra": "pandoras-actor", "gtd": "sebas", "research": "raphael"}
    return {t: seed.get(t) for t in tags}


@activity.defn(name="get_alert_routing_config")
async def stub_get_alert_routing_config() -> dict:
    return {"infra_cluster": ""}


ALL_ACTIVITIES = [
    stub_resolve_agents,
    stub_get_alert_routing_config,
    stub_check_alert_mute,
    stub_write_alert_mute,
    stub_check_dedup,
    stub_find_open_task_for_signature,
    stub_record_signature_new_task,
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
    stub_resolve_interaction,
    stub_apply_timeout,
    stub_stage_pending_pr,
    stub_create_github_pr,
]


def _safe(text: str, max_len: int = 60) -> str:
    return re.sub(r"[^a-zA-Z0-9._\-]", "-", text)[:max_len]


def _make_alert(**overrides) -> dict:
    base = {
        "title": "equities pipeline timeout",
        "fingerprint": "fp-hint-1",
        "severity": "error",
        "source": "todoist-chat",
        "service": "tsp",
        "description": "equities_fundamentals_pipeline step timed out",
        "labels": {},
        "raw_payload": {},
        "todoist_task_id": "TRACK_TASK_1",
    }
    base.update(overrides)
    return base


async def _wait_running(client, child_id: str, attempts: int = 200) -> bool:
    """Poll until the child workflow with `child_id` is RUNNING."""
    child = client.get_workflow_handle(child_id)
    for _ in range(attempts):
        try:
            desc = await child.describe()
        except Exception:
            await asyncio.sleep(0.05)
            continue
        if desc.status is not None and desc.status.name == "RUNNING":
            return True
        await asyncio.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hint_loop_then_pick():
    """not-confident → Gate-0 round 0 → reply `hint:owner/repo` →
    reresolve_with_hint called → round 1 → pick index 0 (the hint candidate) →
    investigation runs against owner/repo, and check_active_work is NEVER called
    (guard bypassed because the operator hand-picked the repo)."""
    _reset()
    parent_id = f"alert-hint-pick-{uuid4().hex[:8]}"
    round0_id = f"repo-confirm-{_safe('fp-hint-1')}-{parent_id}"
    round1_id = f"repo-confirm-{_safe('fp-hint-1')}-h1-{parent_id}"

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq-hint",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_ACTIVITIES,
        ),
    ):
        # `todoist-jira` source → Gate-2 is skipped (scoping-only), so after the
        # human pick the flow runs straight to its terminal return under
        # start_local without a 48h Gate-2 archive wait blocking it.
        handle = await env.client.start_workflow(
            AlertInvestigationFlow.run,
            _make_alert(source="todoist-jira"),
            id=parent_id,
            task_queue="tq-hint",
        )

        # Round 0 → send a hint.
        assert await _wait_running(env.client, round0_id), "round-0 repo-confirm never ran"
        round0 = env.client.get_workflow_handle(round0_id)
        await round0.signal(InteractionFlow.submit_response, {"value": "hint:owner/repo"})

        # Round 1 (post-reresolve) → pick the hint candidate at index 0.
        assert await _wait_running(env.client, round1_id), "round-1 repo-confirm never ran"
        round1 = env.client.get_workflow_handle(round1_id)
        await round1.signal(InteractionFlow.submit_response, {"value": "0"})

        result = await asyncio.wait_for(handle.result(), timeout=20.0)

    # The hint round-tripped through reresolve_with_hint.
    assert _state["reresolve_calls"] == ["owner/repo"]

    # The flow investigated the operator-picked repo.
    assert _state["run_investigation_called"] is True
    resources = _state["run_investigation_resources"]
    assert resources and resources[0]["github_repo"] == "owner/repo"
    assert result["status"] != "repo_unconfirmed"
    assert result["status"] != "skipped_active_work"

    # GUARD BYPASS: a human pick is an explicit "investigate anyway" — the
    # active-work check must NOT run.
    assert _state["check_active_work_called"] is False


@pytest.mark.asyncio
async def test_allow_hint_false_once_max_rounds_reached():
    """After _MAX_HINT_ROUNDS (3) hint replies, the card for the next round must
    be built with allow_hint=False. The flow caps hint rounds; the loop keeps
    going but the hint affordance is withdrawn. We assert via the per-round
    allow_hint flags captured in the send_interaction_card stub."""
    _reset()
    parent_id = f"alert-hint-cap-{uuid4().hex[:8]}"
    fp = _safe("fp-hint-1")
    # round 0 (no suffix), then -h1, -h2, -h3 after each accepted hint.
    round_ids = [
        f"repo-confirm-{fp}-{parent_id}",
        f"repo-confirm-{fp}-h1-{parent_id}",
        f"repo-confirm-{fp}-h2-{parent_id}",
        f"repo-confirm-{fp}-h3-{parent_id}",
    ]

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq-hint",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_ACTIVITIES,
        ),
    ):
        handle = await env.client.start_workflow(
            AlertInvestigationFlow.run,
            _make_alert(),
            id=parent_id,
            task_queue="tq-hint",
        )

        # Send a hint at rounds 0,1,2 (3 accepted hints = _MAX_HINT_ROUNDS).
        for i in range(3):
            assert await _wait_running(env.client, round_ids[i]), f"round-{i} never ran"
            h = env.client.get_workflow_handle(round_ids[i])
            await h.signal(InteractionFlow.submit_response, {"value": f"hint:try-{i}"})

        # Round 3 (round_n == _MAX_HINT_ROUNDS): allow_hint must now be False.
        assert await _wait_running(env.client, round_ids[3]), "round-3 (capped) never ran"
        h3 = env.client.get_workflow_handle(round_ids[3])
        # End the loop deterministically: cancel.
        await h3.signal(InteractionFlow.submit_response, {"value": "none"})

        result = await asyncio.wait_for(handle.result(), timeout=25.0)

    # 3 hints accepted → reresolve called exactly 3×.
    assert _state["reresolve_calls"] == ["try-0", "try-1", "try-2"]

    # Per-round allow_hint flags: rounds 0,1,2 → True; round 3 → False.
    # (Gate-2 never fires — the flow aborts as repo_unconfirmed on the `none`
    # pick — so the captured flags are exactly the four Gate-0 cards.)
    flags = _state["card_allow_hints"]
    assert flags == [True, True, True, False], flags

    # `none` at the capped round → no repo confirmed → clean abort.
    assert result["status"] == "repo_unconfirmed"
    assert _state["run_investigation_called"] is False
    assert _state["check_active_work_called"] is False
