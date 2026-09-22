"""AlertInvestigationFlow Gate-2 "Run fix" — approve-to-run infra remediation.

When an infra investigation's transcript ends in a `PROPOSED_COMMANDS:` footer
(Task 8's `extract_proposed_commands`), the Gate-2 decision card offers a
`run_fix` option alongside mute/ack. Approving it runs the proposed commands
via `AlertActivities.run_remediation_commands`, posts the outcome to the
track-task + chat, waits 180s, and re-checks alert resolution.

Harness copied from test_alert_escalation.py (same stubs, same escalating
infra alert — Gate-0 is skipped for infra alerts so Gate-2's insert_interaction
is the first/only one), with the run_investigation stub's output carrying a
PROPOSED_COMMANDS footer and a `run_remediation_commands` stub added.
"""

from __future__ import annotations

import asyncio
import re

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.alerts import is_read_only_command
    from aegis_worker.activities.interactions import (
        ApplyTimeoutInput,
        InsertInteractionInput,
        InsertInteractionResult,
        ResolveInteractionInput,
        ResolveInteractionResult,
    )
    from aegis_worker.flows.alert_investigation import AlertInvestigationFlow
    from aegis_worker.flows.interaction import InteractionFlow


# ---------------------------------------------------------------------------
# Mutable test state
# ---------------------------------------------------------------------------

_calls: dict = {}
_state: dict = {}


def _esc_alert() -> dict:
    return {
        "title": "Swarm node noon down",
        "fingerprint": "aegis-heartbeat:NodeDown:noon",
        "severity": "critical",
        "source": "aegis-heartbeat",
        "labels": {"alertname": "NodeDown", "cluster": "homelab-swarm"},
        "escalate": True,
    }


def _reset(**overrides):
    _hub_reset()
    _calls.clear()
    _state.clear()
    _state.update(
        {
            # Post-fix verification succeeds — the run_fix branch's
            # check_alert_resolved(fingerprint, 5) call after the 180s wait.
            "resource_result": {
                "resource_id": "res-homelab",
                "resource_title": "Homelab GitOps",
                "resource_path": "infrastructure/homelab-gitops",
                "github_repo": "youruser/homelab-gitops",
                "confidence": 0.9,
                "source": "infra",
                "resources": [
                    {
                        "resource_id": "res-homelab",
                        "resource_title": "Homelab GitOps",
                        "resource_path": "infrastructure/homelab-gitops",
                        "github_repo": "youruser/homelab-gitops",
                        "confidence": 0.9,
                    }
                ],
            },
            "routing": {"infra_cluster": "homelab-swarm", "slack_owner_member_id": "U042"},
            "knowledge_result": "",
            "run_investigation_result": {
                "status": "succeeded",
                "output": (
                    "Root cause: noon NIC flapped\n"
                    "PROPOSED_COMMANDS:\n"
                    "- docker --context swarm service update --force svc_a\n"
                ),
                "session_id": "sess-1",
                "branch": "",
                "branches": {},
                "host": "meem",
            },
            "investigate_result": {
                "investigation": "LLM-only narrative",
                "actionable": True,
                "auto_fixable": False,
            },
            "assess_result": {
                "status": "actionable",
                "root_cause": "noon NIC flapped",
                "suggested_fix": "check the switch port",
                "confidence": 0.8,
            },
        }
    )
    _state.update(overrides)


# ---------------------------------------------------------------------------
# Stub activities
# ---------------------------------------------------------------------------


@activity.defn(name="resolve_agents")
async def stub_resolve_agents(tags):
    return {t: {"infra": "pandoras-actor"}.get(t) for t in tags}


@activity.defn(name="get_alert_routing_config")
async def stub_get_alert_routing_config() -> dict:
    return _state["routing"]


@activity.defn(name="resolve_infra_resource")
async def stub_resolve_infra_resource(alert: dict) -> dict:
    _calls.setdefault("infra_resource_called", []).append(True)
    return _state["resource_result"]


@activity.defn(name="resolve_alert_resource")
async def stub_resolve_alert_resource(alert: dict) -> dict:
    _calls.setdefault("resource_called", []).append(True)
    return _state["resource_result"]


@activity.defn(name="remediate_infra_service")
async def stub_remediate_infra_service(alert: dict) -> dict:
    # NodeDown is not a remediable swarm-service class → no auto-restart kick.
    _calls.setdefault("remediate_called", []).append(True)
    return {"attempted": False}


@activity.defn(name="score_resource_relevance")
async def stub_score_resource_relevance(alert: dict, resolved_resource_id: str) -> dict:
    return {"confident": True, "resolved_resource_id": resolved_resource_id, "candidates": []}


@activity.defn(name="gather_alert_knowledge")
async def stub_gather_alert_knowledge(title: str, project: str, alert_name: str = "") -> str:
    return _state["knowledge_result"]


@activity.defn(name="investigate")
async def stub_investigate(alert: dict, system_prompt: str) -> dict:
    _calls.setdefault("investigate_called", []).append(True)
    return _state["investigate_result"]


@activity.defn(name="run_investigation")
async def stub_run_investigation(alert: dict, resources: list[dict], runbook: str, *_a) -> dict:
    _calls.setdefault("run_investigation_called", []).append(True)
    return _state["run_investigation_result"]


@activity.defn(name="assess_investigation")
async def stub_assess_investigation(alert: dict, investigation_output: str) -> dict:
    _calls.setdefault("assess_called", []).append(True)
    return _state["assess_result"]


@activity.defn(name="record_verdict_to_kg")
async def stub_record_verdict_to_kg(*args, **kwargs) -> None:
    return None


@activity.defn(name="post_task_note")
async def stub_post_task_note(*args, **kwargs) -> dict:
    _calls.setdefault("notes", []).append(args)
    return {}


@activity.defn(name="send_system_event")
async def stub_send_system_event(msg: str) -> None:
    pass


@activity.defn(name="send_message")
async def stub_send_message(
    agent_id: str, msg: str, chat_id: int, reply_markup: dict | None = None
) -> dict:
    _calls.setdefault("messages", []).append(msg)
    return {"ok": True}


@activity.defn(name="send_voice")
async def stub_send_voice(agent_id: str, text: str) -> dict:
    return {"ok": True}


@activity.defn(name="run_remediation_commands")
async def stub_run_remediation_commands(
    commands: list[str], host: str = "", kind: str = "fix"
) -> dict:
    _calls.setdefault("run_remediation_args", []).append((commands, host))
    _calls.setdefault("run_remediation_kind", []).append(kind)
    # The hub sees the problem resolve after the run when the test says so.
    _HUB["resolved"] = _state.get("resolve_after_run", False)
    exits = _state.get("exits", {})
    return {
        "ran": [
            {
                "command": c,
                "exit_code": exits.get(c, 0),
                "stdout": "ok",
                "stderr": "",
                "read_only": is_read_only_command(c),
            }
            for c in commands
        ],
        "refused": None,
    }


# --- InteractionFlow activities ---


@activity.defn(name="insert_interaction")
async def stub_insert_interaction(inp: InsertInteractionInput) -> InsertInteractionResult:
    _calls.setdefault("insert_inputs", []).append(inp)
    return InsertInteractionResult(interaction_id="ia-runfix-test")


@activity.defn(name="send_interaction_card")
async def stub_send_card(
    interaction_id: str,
    agent_id: str,
    kind: str,
    prompt: str,
    options,
    allow_hint: bool = False,
) -> dict:
    _calls.setdefault("cards", []).append(prompt)
    return {"ok": True, "message_id": 42}


@activity.defn(name="resolve_interaction")
async def stub_resolve(inp: ResolveInteractionInput) -> ResolveInteractionResult:
    return ResolveInteractionResult(already_resolved=False)


@activity.defn(name="apply_interaction_timeout")
async def stub_timeout(inp: ApplyTimeoutInput) -> None:
    return None


# ── problem hub stubs (PR 3b) ───────────────────────────────────────────────
# The flow no longer owns an alert's identity: it asks the hub. These stand in
# for HubActivities; `_HUB["resolved"]` makes `problem_status` report the
# problem as resolved, `_HUB["investigate"]` is what `ingest_alert` answers.
_HUB: dict = {
    "ingest": [],
    "status": [],
    "record": [],
    "mute": [],
    "resolved": False,
    "investigate": True,
    "delay": 0,
}


@activity.defn(name="ingest_alert")
async def stub_ingest_alert(alert: dict, resolved: bool = False) -> dict:
    _HUB["ingest"].append((alert.get("fingerprint"), resolved))
    return {
        "problem_id": "prob-1",
        "action": "created",
        "key": "k",
        "occurrences": 1,
        "suppressed": False,
        "investigate": _HUB["investigate"],
        "todoist_task_id": alert.get("todoist_task_id") or "task-hub-1",
    }


@activity.defn(name="problem_status")
async def stub_problem_status(problem_id: str) -> dict:
    _HUB["status"].append(problem_id)
    return {
        "found": True,
        "status": "resolved" if _HUB["resolved"] else "open",
        "resolved": _HUB["resolved"],
        "occurrences": 1,
        "todoist_task_id": "task-hub-1",
    }


@activity.defn(name="record_investigation")
async def stub_record_investigation(inp: dict) -> dict:
    _HUB["record"].append(inp)
    return {"recorded": True, "status_changed": True}


@activity.defn(name="mute_problem")
async def stub_mute_problem(problem_id: str, hours: float, by: str = "") -> dict:
    _HUB["mute"].append((problem_id, hours))
    return {"muted_until": "2026-09-08T12:00:00+00:00"}


@activity.defn(name="verification_delay")
async def stub_verification_delay(alert: dict) -> dict:
    return {"delay_seconds": _HUB["delay"]}


def _hub_reset() -> None:
    for key in ("ingest", "status", "record", "mute"):
        _HUB[key].clear()
    _HUB["resolved"] = False
    _HUB["investigate"] = True
    _HUB["delay"] = 0


ALL_STUBS = [
    stub_ingest_alert,
    stub_problem_status,
    stub_record_investigation,
    stub_mute_problem,
    stub_verification_delay,
    stub_resolve_agents,
    stub_get_alert_routing_config,
    stub_resolve_infra_resource,
    stub_resolve_alert_resource,
    stub_remediate_infra_service,
    stub_score_resource_relevance,
    stub_gather_alert_knowledge,
    stub_investigate,
    stub_run_investigation,
    stub_assess_investigation,
    stub_record_verdict_to_kg,
    stub_post_task_note,
    stub_send_system_event,
    stub_send_message,
    stub_send_voice,
    stub_run_remediation_commands,
    stub_insert_interaction,
    stub_send_card,
    stub_resolve,
    stub_timeout,
]


_SAFE_FINGERPRINT = re.sub(r"[^a-zA-Z0-9._\-]", "-", "aegis-heartbeat:NodeDown:noon")[:60]


async def _wait_for_gate2(poll) -> None:
    """Poll until Gate-2's insert_interaction fired (the first + only insert on
    the infra path, since Gate-0 is skipped for infra alerts)."""
    for _ in range(400):
        if _calls.get("insert_inputs"):
            return
        await poll()
    raise AssertionError("Gate 2 child never started")


async def _run_to_gate2_and_signal(response: dict, wf_id: str) -> dict:
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq-runfix",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_STUBS,
        ),
    ):
        handle = await env.client.start_workflow(
            AlertInvestigationFlow.run,
            _esc_alert(),
            id=wf_id,
            task_queue="tq-runfix",
        )

        await _wait_for_gate2(lambda: env.sleep(1))

        gate2_id = f"gate2-{_SAFE_FINGERPRINT}-{wf_id}"
        gate2_handle = env.client.get_workflow_handle(gate2_id)
        await gate2_handle.signal(InteractionFlow.submit_response, response)

        return await asyncio.wait_for(handle.result(), timeout=30.0)


def _output(footer: str) -> dict:
    return {**_state["run_investigation_result"], "output": "Root cause: noon NIC flapped\n" + footer}


# ---------------------------------------------------------------------------
# Run fix: a change that ran and cleared the problem is `remediated`
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_infra_gate2_run_fix_executes_and_reports():
    _reset(resolve_after_run=True)

    result = await _run_to_gate2_and_signal({"value": "run_fix"}, "runfix-executes-test")

    assert _calls["run_remediation_args"][0][0] == [
        "docker --context swarm service update --force svc_a"
    ]
    assert _calls["run_remediation_args"][0][1] == "meem"
    assert _calls["run_remediation_kind"] == ["fix"]
    assert result["status"] == "remediated"
    gate_insert = _calls["insert_inputs"][-1]
    assert "service update --force svc_a" in gate_insert.prompt
    assert "run_fix" in gate_insert.options
    assert "run_checks" not in gate_insert.options
    assert any("cleared after the fix" in n[1] for n in _calls["notes"])


@pytest.mark.asyncio
async def test_a_fix_that_did_not_clear_the_problem_is_not_remediated():
    """#641: the fix ran, but the hub still sees the problem. That waits on a
    person; it is not a remediation."""
    _reset()

    result = await _run_to_gate2_and_signal({"value": "run_fix"}, "runfix-not-cleared-test")

    assert result["status"] == "waiting_human"


@pytest.mark.asyncio
async def test_a_failed_fix_is_remediation_failed():
    _reset(
        resolve_after_run=True,
        exits={"docker --context swarm service update --force svc_a": 1},
    )

    result = await _run_to_gate2_and_signal({"value": "run_fix"}, "runfix-failed-test")

    assert result["status"] == "remediation_failed"
    assert any("⚠️ Ran 1 fix:" in m for m in _calls["messages"])


# ---------------------------------------------------------------------------
# A free-text note on the gate overrides the proposed commands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_infra_gate2_note_overrides_commands():
    _reset(resolve_after_run=True)

    result = await _run_to_gate2_and_signal(
        {"value": "run_fix", "note": "docker service update --force svc_b"},
        "runfix-note-override-test",
    )

    assert _calls["run_remediation_args"][0][0] == ["docker service update --force svc_b"]
    assert result["status"] == "remediated"


@pytest.mark.asyncio
async def test_a_note_of_only_checks_is_checked_even_when_the_problem_clears():
    """The problem cleared, but nothing the operator ran changed anything, so
    the run did not remediate it."""
    _reset(resolve_after_run=True)

    result = await _run_to_gate2_and_signal(
        {"value": "run_fix", "note": "docker node ls"}, "runfix-note-checks-test"
    )

    assert _calls["run_remediation_args"][0][0] == ["docker node ls"]
    assert result["status"] == "checked"
    assert any("no fix command succeeded" in n[1] for n in _calls["notes"])


# ---------------------------------------------------------------------------
# #641: the 2026-09-21 card, replayed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checks_get_run_checks_and_a_failed_ping_does_not_count():
    """The wow card listed a ping and inspects as "Proposed fix commands".
    Now they are checks: the card offers Run checks, not Run fix; the run
    goes as kind `check`, a failed ping is reported without ⚠️, nothing waits
    for the problem to clear, and the status is `checked`."""
    _reset(
        exits={"ping -c 3 -W 2 10.20.0.17": 1},
    )
    _state["run_investigation_result"] = {
        "status": "succeeded",
        "output": (
            "wow is off.\n\nFIX_COMMANDS:\n"
            "- ping -c 3 -W 2 10.20.0.17\n"
            "- docker node ls\n"
        ),
        "session_id": "sess-1",
        "branch": "",
        "branches": {},
        "host": "meem",
    }

    result = await _run_to_gate2_and_signal({"value": "run_checks"}, "runchecks-test")

    gate_insert = _calls["insert_inputs"][-1]
    assert "run_fix" not in gate_insert.options
    assert "run_checks" in gate_insert.options
    assert "Proposed fix commands" not in gate_insert.prompt
    assert "Read-only checks (Run checks)" in gate_insert.prompt
    assert _calls["run_remediation_args"][0][0] == ["ping -c 3 -W 2 10.20.0.17", "docker node ls"]
    assert _calls["run_remediation_kind"] == ["check"]
    assert result["status"] == "checked"
    assert result["commands_ran"] == 2
    msg = next(m for m in _calls["messages"] if "Check result" in m)
    assert "🔍 Ran 2 checks:" in msg
    assert "⚠️" not in msg
    assert not any(r.get("status") == "resolved" for r in _HUB["record"])


@pytest.mark.asyncio
async def test_run_checks_ignores_a_note():
    """A note on Run checks could run a change under the name of a check."""
    _reset()
    _state["run_investigation_result"] = _output(
        "CHECK_COMMANDS:\n- docker node ls\n\nFIX_COMMANDS:\n- docker service update --force svc_a\n"
    )

    await _run_to_gate2_and_signal(
        {"value": "run_checks", "note": "docker node rm wow"}, "runchecks-note-test"
    )

    assert _calls["run_remediation_args"][0][0] == ["docker node ls"]


