"""MonthCloseFlow — build, render, notify, file the report, in that order."""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.month_close import MonthCloseConfig, MonthCloseFlow

seen: list = []


@activity.defn(name="build_month_close")
async def stub_build() -> dict:
    seen.append(("build",))
    return {"month": "2026-08", "books_ok": True}


@activity.defn(name="render_month_close")
async def stub_render(close: dict) -> dict:
    seen.append(("render", close["month"]))
    return {"html": "<b>c</b>", "markdown": "# c"}


@activity.defn(name="notify_money_message")
async def stub_notify(html: str, log_event: str) -> bool:
    seen.append(("notify", log_event))
    return True


@activity.defn(name="notify_money_message")
async def stub_notify_undelivered(html: str, log_event: str) -> bool:
    seen.append(("notify", log_event))
    return False


@activity.defn(name="write_money_report")
async def stub_report(rel_path: str, text: str) -> None:
    seen.append(("report", rel_path))
    seen.append(("report_body", text))


async def _run(config, stubs, wid):
    seen.clear()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="tq", workflows=[MonthCloseFlow], activities=stubs),
    ):
        return await env.client.execute_workflow(
            MonthCloseFlow.run, config, id=wid, task_queue="tq"
        )


@pytest.mark.asyncio
async def test_month_close_flow():
    result = await _run(
        MonthCloseConfig(), [stub_build, stub_render, stub_notify, stub_report], "mc-1"
    )
    assert result == {"month": "2026-08", "sent": True, "books_ok": True}
    assert seen == [
        ("build",),
        ("render", "2026-08"),
        ("notify", "month_close_notify_failed"),
        ("report", "reports/monthly/2026-08.md"),
        ("report_body", "# c"),
    ]


@pytest.mark.asyncio
async def test_month_close_flow_silent_still_files_the_report():
    result = await _run(
        MonthCloseConfig(silent=True), [stub_build, stub_render, stub_notify, stub_report], "mc-2"
    )
    assert result == {"month": "2026-08", "sent": False, "books_ok": True}
    assert ("notify", "month_close_notify_failed") not in seen
    assert ("report", "reports/monthly/2026-08.md") in seen


@pytest.mark.asyncio
async def test_month_close_flow_reports_sent_false_when_delivery_failed():
    result = await _run(
        MonthCloseConfig(),
        [stub_build, stub_render, stub_notify_undelivered, stub_report],
        "mc-3",
    )
    assert result == {"month": "2026-08", "sent": False, "books_ok": True}
    assert ("notify", "month_close_notify_failed") in seen
    assert ("report", "reports/monthly/2026-08.md") in seen


@pytest.mark.asyncio
async def test_month_close_flow_reports_a_books_outage():
    """The index half of the close still ships when hledger is unreachable, and
    `books_ok` rides through so the outage is visible in `workflow_runs`."""

    @activity.defn(name="build_month_close")
    async def build_no_books() -> dict:
        seen.append(("build",))
        return {"month": "2026-08", "books_ok": False}

    result = await _run(
        MonthCloseConfig(), [build_no_books, stub_render, stub_notify, stub_report], "mc-4"
    )
    assert result == {"month": "2026-08", "sent": True, "books_ok": False}
    assert ("report", "reports/monthly/2026-08.md") in seen
