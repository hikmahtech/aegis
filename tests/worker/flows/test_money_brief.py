"""MoneyBriefFlow — the weekly brief's step ORDER, its silent mode, its
refusal to die when the FX quote provider is down, and its honesty about
whether the message actually reached the user.

Every stub records into one shared `seen` list, so the assertions pin the
sequence. Five separate per-activity lists would leave "file the report before
sending it" green.
"""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.money_brief import MoneyBriefConfig, MoneyBriefFlow

seen: list = []


@activity.defn(name="refresh_fx_prices")
async def stub_fx() -> dict:
    seen.append(("fx",))
    return {"written": 3, "errors": []}


@activity.defn(name="refresh_fx_prices")
async def stub_fx_boom() -> dict:
    raise RuntimeError("quotes down")


@activity.defn(name="build_money_brief")
async def stub_build(days: int = 7) -> dict:
    seen.append(("build", days))
    return {"as_of": "2026-09-06", "books_ok": True, "dues": [1, 2], "unknowns": [1]}


@activity.defn(name="render_money_brief")
async def stub_render(brief: dict) -> dict:
    seen.append(("render", brief["as_of"]))
    return {"html": "<b>x</b>", "markdown": "# x"}


@activity.defn(name="notify_money_message")
async def stub_notify(html: str, log_event: str) -> bool:
    seen.append(("notify", html, log_event))
    return True


@activity.defn(name="notify_money_message")
async def stub_notify_undelivered(html: str, log_event: str) -> bool:
    """`safe_send_message` swallowed a raise or an `ok=false`."""
    seen.append(("notify", html, log_event))
    return False


@activity.defn(name="write_money_report")
async def stub_report(rel_path: str, text: str) -> None:
    seen.append(("report", rel_path, text))


async def _run(config, stubs, wid):
    seen.clear()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="tq", workflows=[MoneyBriefFlow], activities=stubs),
    ):
        return await env.client.execute_workflow(
            MoneyBriefFlow.run, config, id=wid, task_queue="tq"
        )


@pytest.mark.asyncio
async def test_brief_flow_end_to_end_in_order():
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
    # Prices, then the data, then the render, then the message, then the file.
    assert seen == [
        ("fx",),
        ("build", 14),
        ("render", "2026-09-06"),
        ("notify", "<b>x</b>", "money_brief_notify_failed"),
        ("report", "reports/weekly/2026-09-06.md", "# x"),
    ]


@pytest.mark.asyncio
async def test_brief_flow_survives_fx_failure_and_silent():
    result = await _run(
        MoneyBriefConfig(silent=True),
        [stub_fx_boom, stub_build, stub_render, stub_notify, stub_report],
        "mb-2",
    )
    assert result["sent"] is False
    # Stale prices are still worth a brief: the run continues and files it.
    assert seen == [
        ("build", 7),
        ("render", "2026-09-06"),
        ("report", "reports/weekly/2026-09-06.md", "# x"),
    ]


@pytest.mark.asyncio
async def test_brief_flow_reports_sent_false_when_delivery_failed():
    """`sent` is delivery, not dispatch. Slack being down must not be recorded
    in `workflow_runs` as a brief the user received."""
    result = await _run(
        MoneyBriefConfig(),
        [stub_fx, stub_build, stub_render, stub_notify_undelivered, stub_report],
        "mb-4",
    )
    assert result["sent"] is False
    assert ("notify", "<b>x</b>", "money_brief_notify_failed") in seen
    # The report is still filed — an undelivered message is not a failed run.
    assert ("report", "reports/weekly/2026-09-06.md", "# x") in seen


@pytest.mark.asyncio
async def test_brief_flow_defaults_and_reports_a_books_outage():
    """books_ok=False rides through to the result, so a books outage is visible
    in `workflow_runs` instead of only in a log line."""

    @activity.defn(name="build_money_brief")
    async def build_no_books(days: int = 7) -> dict:
        seen.append(("build", days))
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
    assert ("build", 7) in seen
