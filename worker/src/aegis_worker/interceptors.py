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

import json
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

# ~16KB serialized cap on result_summary/input_summary (issue #112) — big enough
# for legitimate distributional fields (by_category, per-feed breakdowns) while
# stopping one pathological result from bloating workflow_runs indefinitely.
_MAX_SUMMARY_BYTES = 16_000

_JSON_PRIMITIVES = (str, int, float, bool, type(None))


def _json_safe(value: Any) -> Any:
    """Recursively coerce a value into native JSON types (dict/list/str/int/float/bool/None).

    The asyncpg jsonb codec's encoder (`db/pool.py::_encode_jsonb`) calls bare
    `json.dumps(value)` with no `default=` — so any leaf that isn't already a
    JSON-native type (e.g. a datetime nested inside a dict) would raise
    mid-activity and turn an otherwise-successful workflow into a spurious
    failure. Non-native leaves are stringified here, up front, via `str()`.
    """
    if isinstance(value, _JSON_PRIMITIVES):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


def _cap_summary_size(d: dict[str, Any], max_bytes: int = _MAX_SUMMARY_BYTES) -> dict[str, Any]:
    """Drop the largest top-level values (by their own serialized size) until
    the whole dict serializes under `max_bytes`. Values are assumed already
    JSON-safe (see `_json_safe`), so plain `json.dumps` is enough here."""
    if len(json.dumps(d)) <= max_bytes:
        return d
    out = dict(d)
    sizes = {k: len(json.dumps(v)) for k, v in d.items()}
    for k in sorted(sizes, key=lambda key: sizes[key], reverse=True):
        out[k] = f"<dropped: {sizes[k]} bytes>"
        if len(json.dumps(out)) <= max_bytes:
            break
    return out


def _summarise(obj: Any) -> dict[str, Any] | None:
    """Best-effort summary of a dataclass or plain dict (first arg or return value).

    Nested dict/list values pass through natively as real JSON objects/arrays
    (not `str(v)` reprs) so SQL (`->`, `->>`, `jsonb_each`) can query them —
    see issue #112. Every leaf is coerced to a JSON-native type first
    (`_json_safe`), then the whole summary is capped at ~16KB serialized
    (`_cap_summary_size`) so a pathological result can't bloat the table.
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
    safe = {str(k): _json_safe(v) for k, v in d.items()}
    return _cap_summary_size(safe)


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
