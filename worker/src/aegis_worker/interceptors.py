"""Temporal WorkerInterceptor for workflow_runs.

Wraps every workflow's execute_workflow. Extracts agent_id from the first
arg's .agent_id attribute (v3 convention; see CLAUDE.md). Records a
single workflow_runs row at the END of execution (success or failure)
via a fire-and-forget activity with a short retry budget.

Why a single terminal write (not start+complete): calling
`workflow.execute_activity` from inside the interceptor BEFORE
`self.next.execute_workflow(input)` segfaults asyncpg under the
Temporal `start_time_skipping` dev server (reproduced 2026-04-17,
temporalio 1.26.0, asyncpg 0.31.0). Post-run activity calls work
cleanly, so we record the terminal state once.

This sacrifices "running" visibility for short workflows — an acceptable
trade since Temporal itself is the state of record; workflow_runs is
an aggregation view for dashboards. Long-running workflows that need
mid-flight visibility can emit their own progress rows via regular
activities.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.worker import (
    ExecuteWorkflowInput,
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.runs_v3 import RecordRunInput


_ACT_RETRY = RetryPolicy(maximum_attempts=2)
_ACT_TIMEOUT = timedelta(seconds=30)


def _summarise(obj: Any) -> dict[str, Any] | None:
    """Best-effort summary of a dataclass or plain dict (first arg or return value).

    Primitives pass through; everything else stringified and capped at 500 chars
    so the JSONB column doesn't balloon.
    """
    if obj is None:
        return None
    if is_dataclass(obj):
        try:
            d = asdict(obj)
        except Exception:
            return {"type": type(obj).__name__}
    elif isinstance(obj, dict):
        d = obj
    else:
        return {"type": type(obj).__name__}
    return {
        k: (v if isinstance(v, (int, float, bool, str, type(None))) else str(v)[:500])
        for k, v in d.items()
    }


def _extract_agent_id(args: tuple[Any, ...]) -> str | None:
    if not args:
        return None
    first = args[0]
    if isinstance(first, dict):
        return first.get("agent_id")
    return getattr(first, "agent_id", None)


def _extract_todoist_task_ref(args: tuple[Any, ...]) -> str | None:
    """Pull todoist_task_id off the first argument when present.

    Supported shapes:
      - dict input (e.g. AlertInvestigationFlow's `alert` dict).
      - dataclass input with a `todoist_task_id` attribute.

    None when the workflow has no task anchor — the column on
    workflow_runs is nullable.
    """
    if not args:
        return None
    first = args[0]
    if isinstance(first, dict):
        v = first.get("todoist_task_id")
    else:
        v = getattr(first, "todoist_task_id", None)
    if not v:
        return None
    s = str(v).strip()
    return s or None


class _Inbound(WorkflowInboundInterceptor):
    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        started_at = workflow.now()
        args_ref = input.args

        try:
            result = await self.next.execute_workflow(input)
        except BaseException as exc:
            agent_id = _extract_agent_id(args_ref)
            todoist_task_ref = _extract_todoist_task_ref(args_ref)
            input_summary = _summarise(args_ref[0] if args_ref else None)
            completed_at = workflow.now()
            duration_ms = int((completed_at - started_at).total_seconds() * 1000)
            err = f"{type(exc).__name__}: {exc}"
            # Surface the reason in result_summary so failed runs are debuggable
            # without round-tripping to Temporal UI history. Flows that want
            # richer step-context should raise ApplicationError with a
            # `f"... at step=X: ..."` message — that propagates here through
            # the standard error string.
            failure_summary = {
                "reason": str(exc)[:1000],
                "exception_type": type(exc).__name__,
            }
            info = workflow.info()
            try:
                await workflow.execute_activity(
                    "record_workflow_run",
                    RecordRunInput(
                        run_id=info.run_id,
                        workflow_id=info.workflow_id,
                        workflow_type=info.workflow_type,
                        agent_id=agent_id,
                        parent_run_id=info.parent.run_id if info.parent else None,
                        started_at=started_at,
                        completed_at=completed_at,
                        duration_ms=duration_ms,
                        status="failed",
                        error=err[:2000],
                        input_summary=input_summary,
                        result_summary=failure_summary,
                        todoist_task_ref=todoist_task_ref,
                    ),
                    retry_policy=_ACT_RETRY,
                    start_to_close_timeout=_ACT_TIMEOUT,
                )
            except Exception as record_exc:
                workflow.logger.warning(
                    "workflow_run_record_failed",
                    extra={
                        "error": str(record_exc),
                        "workflow_type": info.workflow_type,
                        "workflow_id": info.workflow_id,
                        "status": "failed",
                    },
                )
                # Telemetry is best-effort on the failure path
            raise

        agent_id = _extract_agent_id(args_ref)
        todoist_task_ref = _extract_todoist_task_ref(args_ref)
        input_summary = _summarise(args_ref[0] if args_ref else None)
        completed_at = workflow.now()
        duration_ms = int((completed_at - started_at).total_seconds() * 1000)
        info = workflow.info()
        await workflow.execute_activity(
            "record_workflow_run",
            RecordRunInput(
                run_id=info.run_id,
                workflow_id=info.workflow_id,
                workflow_type=info.workflow_type,
                agent_id=agent_id,
                parent_run_id=info.parent.run_id if info.parent else None,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
                status="completed",
                error=None,
                input_summary=input_summary,
                result_summary=_summarise(result),
                todoist_task_ref=todoist_task_ref,
            ),
            retry_policy=_ACT_RETRY,
            start_to_close_timeout=_ACT_TIMEOUT,
        )
        return result


class WorkflowRunRecorderInterceptor(Interceptor):
    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor]:
        return _Inbound
