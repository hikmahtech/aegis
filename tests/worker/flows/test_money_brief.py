"""MoneyBriefFlow — the weekly brief's step order, its silent mode, and its
refusal to die when the FX quote provider is down."""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.money_brief import MoneyBriefConfig, MoneyBriefFlow

calls: dict[str, list] = {k: [] for k in ("fx", "build", "render", "notify", "report")}


@activity.defn(name="refresh_fx_prices")
async def stub_fx() -> dict:
    calls["fx"].append(1)
    return {"written": 3, "errors": []}


@activity.defn(name="refresh_fx_prices")
async def stub_fx_boom() -> dict:
    raise RuntimeError("quotes down")


@activity.defn(name="build_money_brief")
async def stub_build(days: int = 7) -> dict:
    calls["build"].append(days)
    return {"as_of": "2026-09-06", "books_ok": True, "dues": [1, 2], "unknowns": [1]}


@activity.defn(name="render_money_brief")
async def stub_render(brief: dict) -> dict:
    calls["render"].append(brief["as_of"])
    return {"html": "<b>x</b>", "markdown": "# x"}


@activity.defn(name="notify_money_message")
async def stub_notify(html: str, log_event: str) -> None:
    calls["notify"].append((html, log_event))


@activity.defn(name="write_money_report")
async def stub_report(rel_path: str, text: str) -> None:
    calls["report"].append((rel_path, text))


def _reset():
    for v in calls.values():
        v.clear()


async def _run(config, stubs, wid):
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="tq", workflows=[MoneyBriefFlow], activities=stubs),
    ):
        return await env.client.execute_workflow(
            MoneyBriefFlow.run, config, id=wid, task_queue="tq"
        )


@pytest.mark.asyncio
async def test_brief_flow_end_to_end():
    _reset()
    result = await _run(
        MoneyBriefConfig(days=14),
        [stub_fx, stub_build, stub_render, stub_notify, stub_report],
        "mb-1",
    )
    assert result == {
        "as_of": "2026-09-06",
        "sent": True,
        "dues": 2,
        "unknowns": 1,
        "books_ok": True,
    }
    assert calls["build"] == [14] and calls["notify"] == [("<b>x</b>", "money_brief_notify_failed")]
    assert calls["report"] == [("reports/weekly/2026-09-06.md", "# x")]
    # The brief is rendered from what `build_money_brief` returned, not rebuilt.
    assert calls["render"] == ["2026-09-06"]


@pytest.mark.asyncio
async def test_brief_flow_survives_fx_failure_and_silent():
    _reset()
    result = await _run(
        MoneyBriefConfig(silent=True),
        [stub_fx_boom, stub_build, stub_render, stub_notify, stub_report],
        "mb-2",
    )
    assert result["sent"] is False and calls["notify"] == [] and len(calls["report"]) == 1
    # Stale prices are still worth a brief: the FX failure must not stop the run.
    assert calls["build"] == [7]


@pytest.mark.asyncio
async def test_brief_flow_defaults_and_reports_a_books_outage():
    """books_ok=False rides through to the result, so a books outage is visible
    in `workflow_runs` instead of only in a log line."""
    _reset()

    @activity.defn(name="build_money_brief")
    async def build_no_books(days: int = 7) -> dict:
        calls["build"].append(days)
        return {"as_of": "2026-09-06", "books_ok": False, "dues": [], "unknowns": []}

    result = await _run(
        MoneyBriefConfig(),
        [stub_fx, build_no_books, stub_render, stub_notify, stub_report],
        "mb-3",
    )
    assert result == {
        "as_of": "2026-09-06",
        "sent": True,
        "dues": 0,
        "unknowns": 0,
        "books_ok": False,
    }
    assert calls["build"] == [7]
