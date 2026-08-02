"""FlowHealthWatchdogFlow — wiring of the two detectors into one report (#226).

The detectors themselves are exercised against a real database in
tests/worker/activities/test_flow_health.py; this file only proves the flow
hands them the configured knobs, merges both result sets, and degrades instead
of dying when the stale half breaks.
"""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.flow_health import FlowHealthConfig, FlowHealthWatchdogFlow

_failing_calls: list[tuple] = []
_stale_calls: list[tuple] = []
_report_calls: list[tuple] = []

FAILING = {"kind": "failing", "subject": "zzwd-type-a", "consecutive": 2}
STALE = {"kind": "stale", "subject": "zzwd-sched-a", "idle_minutes": 300}


def _make_failing(rows):
    @activity.defn(name="find_failing_flows")
    async def stub(consecutive: int = 2, lookback_hours: int = 24) -> list[dict]:
        _failing_calls.append((consecutive, lookback_hours))
        return rows

    return stub


def _make_stale(rows, boom: bool = False):
    @activity.defn(name="find_stale_flows")
    async def stub(multiplier: float = 3.0, min_stale_minutes: int = 60) -> list[dict]:
        _stale_calls.append((multiplier, min_stale_minutes))
        if boom:
            raise RuntimeError("stale scan exploded")
        return rows

    return stub


@activity.defn(name="report_flow_health")
async def stub_report(
    findings: list[dict],
    agent_id: str = "pandoras-actor",
    dedup_hours: int = 12,
    recovery_hours: int = 168,
) -> dict:
    _report_calls.append((findings, agent_id, dedup_hours, recovery_hours))
    return {"alerted": len(findings), "deduped": 0, "muted": 0, "recovered": 0}


async def _run(config, wf_id, failing=(), stale=(), stale_boom=False):
    _failing_calls.clear()
    _stale_calls.clear()
    _report_calls.clear()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[FlowHealthWatchdogFlow],
            activities=[
                _make_failing(list(failing)),
                _make_stale(list(stale), boom=stale_boom),
                stub_report,
            ],
        ),
    ):
        return await env.client.execute_workflow(
            FlowHealthWatchdogFlow.run, config, id=wf_id, task_queue="tq"
        )


@pytest.mark.asyncio
async def test_both_detectors_feed_one_report():
    result = await _run(FlowHealthConfig(), "fh-1", failing=[FAILING], stale=[STALE])
    assert result["failing"] == 1
    assert result["stale"] == 1
    assert result["stale_status"] == "ok"
    assert result["subjects"] == ["zzwd-sched-a", "zzwd-type-a"]
    assert len(_report_calls) == 1
    findings, agent_id, dedup_hours, recovery_hours = _report_calls[0]
    assert [f["subject"] for f in findings] == ["zzwd-type-a", "zzwd-sched-a"]
    assert (agent_id, dedup_hours, recovery_hours) == ("pandoras-actor", 12, 168)
    assert result["alerted"] == 2


@pytest.mark.asyncio
async def test_config_knobs_reach_the_detectors():
    """Literal values, none of them equal to a production default, so a flow
    that ignored the config and passed its own defaults would fail here."""
    cfg = FlowHealthConfig(
        agent_id="sebas",
        consecutive_failures=4,
        lookback_hours=9,
        stale_multiplier=7.5,
        min_stale_minutes=11,
        dedup_hours=5,
        recovery_hours=13,
    )
    await _run(cfg, "fh-2")
    assert _failing_calls == [(4, 9)]
    assert _stale_calls == [(7.5, 11)]
    assert _report_calls[0][1:] == ("sebas", 5, 13)


@pytest.mark.asyncio
async def test_report_runs_even_with_no_findings():
    """Zero findings is exactly when a recovery notice fires — skipping the
    report on an empty sweep would make recovery unobservable."""
    result = await _run(FlowHealthConfig(), "fh-3")
    assert len(_report_calls) == 1
    assert _report_calls[0][0] == []
    assert result["failing"] == 0 and result["stale"] == 0


@pytest.mark.asyncio
async def test_a_broken_stale_scan_still_delivers_the_failure_alert():
    """Degradation: the stale half is best-effort, and its breakage is
    recorded in result_summary rather than swallowed."""
    result = await _run(FlowHealthConfig(), "fh-4", failing=[FAILING], stale_boom=True)
    assert result["stale_status"] == "check_failed"
    assert result["failing"] == 1
    assert result["stale"] == 0
    assert len(_report_calls) == 1
    assert [f["subject"] for f in _report_calls[0][0]] == ["zzwd-type-a"]


@pytest.mark.asyncio
async def test_check_stale_false_skips_the_scan():
    result = await _run(FlowHealthConfig(check_stale=False), "fh-5", stale=[STALE])
    assert _stale_calls == []
    assert result["stale_status"] == "disabled"
    assert result["stale"] == 0


@pytest.mark.asyncio
async def test_silent_detects_but_never_notifies():
    result = await _run(
        FlowHealthConfig(silent=True), "fh-6", failing=[FAILING], stale=[STALE]
    )
    assert _report_calls == []
    assert result["silent"] is True
    assert result["failing"] == 1 and result["stale"] == 1
