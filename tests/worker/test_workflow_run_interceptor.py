"""Interceptor populates workflow_runs on complete / fail."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from temporalio import workflow
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

# Imports that transitively pull in asyncpg MUST go through imports_passed_through
# or the workflow sandbox corrupts asyncpg's C extensions and later DB calls segfault.
with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.runs_v3 import RunRecorderActivities
    from aegis_worker.interceptors import (
        _MAX_SUMMARY_BYTES,
        WorkflowRunRecorderInterceptor,
        _cap_summary_size,
        _json_safe,
        _summarise,
    )


@dataclass
class _OkInput:
    agent_id: str


@workflow.defn(name="_OkFlow")
class _OkFlow:
    @workflow.run
    async def run(self, input: _OkInput) -> str:
        return "ok"


@workflow.defn(name="_FailFlow")
class _FailFlow:
    @workflow.run
    async def run(self, input: _OkInput) -> str:
        raise ApplicationError("nope")


@dataclass
class _Barebones:
    some_other_field: int


@workflow.defn(name="_BarebonesFlow")
class _BarebonesFlow:
    @workflow.run
    async def run(self, input: _Barebones) -> str:
        return "ok"


@pytest_asyncio.fixture(loop_scope="function")
async def temporal_env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


@pytest_asyncio.fixture(loop_scope="function")
async def seeded_agent(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, role, system_prompt_path, active) "
            "VALUES ('sebas', 'S', 'a', 'p', TRUE) ON CONFLICT DO NOTHING"
        )
    yield
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM workflow_runs")


async def test_interceptor_records_success_run(
    temporal_env: WorkflowEnvironment, db_pool, seeded_agent
):
    recorder = RunRecorderActivities(db_pool=db_pool)
    tq = f"test-{uuid4().hex[:8]}"

    async with Worker(
        temporal_env.client,
        task_queue=tq,
        workflows=[_OkFlow],
        activities=[recorder.record_workflow_run],
        interceptors=[WorkflowRunRecorderInterceptor()],
    ):
        await temporal_env.client.execute_workflow(
            _OkFlow.run,
            _OkInput(agent_id="sebas"),
            id=f"ok-{uuid4()}",
            task_queue=tq,
        )

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT workflow_type, agent_id, status, error, duration_ms "
            "FROM workflow_runs WHERE workflow_type = '_OkFlow' "
            "ORDER BY started_at DESC LIMIT 1"
        )
    assert len(rows) == 1
    assert rows[0]["workflow_type"] == "_OkFlow"
    assert rows[0]["agent_id"] == "sebas"
    assert rows[0]["status"] == "completed"
    assert rows[0]["error"] is None
    assert rows[0]["duration_ms"] is not None


async def test_interceptor_records_failure_run(temporal_env, db_pool, seeded_agent):
    recorder = RunRecorderActivities(db_pool=db_pool)
    tq = f"test-{uuid4().hex[:8]}"

    async with Worker(
        temporal_env.client,
        task_queue=tq,
        workflows=[_FailFlow],
        activities=[recorder.record_workflow_run],
        interceptors=[WorkflowRunRecorderInterceptor()],
    ):
        with pytest.raises(WorkflowFailureError):
            await temporal_env.client.execute_workflow(
                _FailFlow.run,
                _OkInput(agent_id="sebas"),
                id=f"fail-{uuid4()}",
                task_queue=tq,
            )

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT status, error, result_summary FROM workflow_runs "
            "WHERE workflow_type = '_FailFlow' ORDER BY started_at DESC LIMIT 1"
        )
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert "nope" in (rows[0]["error"] or "")
    # Bundle J: result_summary now carries reason + exception_type so failed
    # runs are debuggable without round-tripping to Temporal UI history.
    import json as _json

    raw = rows[0]["result_summary"]
    summary = _json.loads(raw) if isinstance(raw, str) else raw
    assert summary is not None
    assert "nope" in summary["reason"]
    assert summary["exception_type"] == "ApplicationError"


async def test_interceptor_null_agent_id_when_no_attribute(temporal_env, db_pool):
    """A workflow whose input has no agent_id attribute records agent_id=NULL."""
    recorder = RunRecorderActivities(db_pool=db_pool)
    tq = f"test-{uuid4().hex[:8]}"

    async with Worker(
        temporal_env.client,
        task_queue=tq,
        workflows=[_BarebonesFlow],
        activities=[recorder.record_workflow_run],
        interceptors=[WorkflowRunRecorderInterceptor()],
    ):
        await temporal_env.client.execute_workflow(
            _BarebonesFlow.run,
            _Barebones(some_other_field=42),
            id=f"barebones-{uuid4()}",
            task_queue=tq,
        )

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT agent_id FROM workflow_runs "
            "WHERE workflow_type = '_BarebonesFlow' ORDER BY started_at DESC LIMIT 1"
        )
    assert row is not None
    assert row["agent_id"] is None

    # Cleanup
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM workflow_runs WHERE workflow_type = '_BarebonesFlow'")


# ── todoist_task_ref extraction (PR fix, 2026-05-21) ────────────────


@workflow.defn(name="_DictArgFlow")
class _DictArgFlow:
    """Mimics AlertInvestigationFlow — first arg is a dict with todoist_task_id."""

    @workflow.run
    async def run(self, alert: dict) -> str:
        return "ok"


@dataclass
class _DataclassWithTaskId:
    agent_id: str
    todoist_task_id: str


@workflow.defn(name="_DataclassTaskRefFlow")
class _DataclassTaskRefFlow:
    @workflow.run
    async def run(self, inp: _DataclassWithTaskId) -> str:
        return "ok"


async def test_interceptor_extracts_todoist_task_ref_from_dict_input(
    temporal_env, db_pool, seeded_agent
):
    """AlertInvestigationFlow passes a dict alert with todoist_task_id —
    the interceptor must extract it into workflow_runs.todoist_task_ref."""
    recorder = RunRecorderActivities(db_pool=db_pool)
    tq = f"test-{uuid4().hex[:8]}"

    async with Worker(
        temporal_env.client,
        task_queue=tq,
        workflows=[_DictArgFlow],
        activities=[recorder.record_workflow_run],
        interceptors=[WorkflowRunRecorderInterceptor()],
    ):
        await temporal_env.client.execute_workflow(
            _DictArgFlow.run,
            {"agent_id": "pandoras-actor", "todoist_task_id": "6ggh7w2w8wgCxmgv"},
            id=f"dict-{uuid4()}",
            task_queue=tq,
        )

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT agent_id, todoist_task_ref FROM workflow_runs "
            "WHERE workflow_type = '_DictArgFlow' ORDER BY started_at DESC LIMIT 1"
        )
        await conn.execute("DELETE FROM workflow_runs WHERE workflow_type = '_DictArgFlow'")
    assert row is not None
    assert row["agent_id"] == "pandoras-actor"
    assert row["todoist_task_ref"] == "6ggh7w2w8wgCxmgv"


async def test_interceptor_extracts_todoist_task_ref_from_dataclass_input(
    temporal_env, db_pool, seeded_agent
):
    """A dataclass input with .todoist_task_id should also flow through."""
    recorder = RunRecorderActivities(db_pool=db_pool)
    tq = f"test-{uuid4().hex[:8]}"

    async with Worker(
        temporal_env.client,
        task_queue=tq,
        workflows=[_DataclassTaskRefFlow],
        activities=[recorder.record_workflow_run],
        interceptors=[WorkflowRunRecorderInterceptor()],
    ):
        await temporal_env.client.execute_workflow(
            _DataclassTaskRefFlow.run,
            _DataclassWithTaskId(agent_id="sebas", todoist_task_id="TASK_ABC"),
            id=f"dc-{uuid4()}",
            task_queue=tq,
        )

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT todoist_task_ref FROM workflow_runs "
            "WHERE workflow_type = '_DataclassTaskRefFlow' ORDER BY started_at DESC LIMIT 1"
        )
        await conn.execute(
            "DELETE FROM workflow_runs WHERE workflow_type = '_DataclassTaskRefFlow'"
        )
    assert row is not None
    assert row["todoist_task_ref"] == "TASK_ABC"


async def test_interceptor_null_todoist_task_ref_when_absent(
    temporal_env, db_pool, seeded_agent
):
    """An _OkFlow with no todoist_task_id records NULL — column is nullable."""
    recorder = RunRecorderActivities(db_pool=db_pool)
    tq = f"test-{uuid4().hex[:8]}"

    async with Worker(
        temporal_env.client,
        task_queue=tq,
        workflows=[_OkFlow],
        activities=[recorder.record_workflow_run],
        interceptors=[WorkflowRunRecorderInterceptor()],
    ):
        await temporal_env.client.execute_workflow(
            _OkFlow.run,
            _OkInput(agent_id="sebas"),
            id=f"ok-noref-{uuid4()}",
            task_queue=tq,
        )

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT todoist_task_ref FROM workflow_runs "
            "WHERE workflow_type = '_OkFlow' ORDER BY started_at DESC LIMIT 1"
        )
    assert row is not None
    assert row["todoist_task_ref"] is None


# ── result_summary nested dict/list handling (issue #112) ──────────


def test_summarise_preserves_nested_dict_and_list():
    """A nested dict/list value comes back as a native Python dict/list, not a
    `str(v)` repr — this is the core of issue #112 (by_category/feeds were
    stored as unqueryable jsonb string scalars)."""
    obj = {
        "status": "ok",
        "by_category": {"groceries": 3, "utilities": 1},
        "feeds": [{"name": "a", "count": 1}],
    }
    out = _summarise(obj)
    assert out["by_category"] == {"groceries": 3, "utilities": 1}
    assert out["feeds"] == [{"name": "a", "count": 1}]
    # Must be plain-JSON round-trippable — this is exactly what asyncpg's
    # jsonb codec does with no `default=` (db/pool.py::_encode_jsonb).
    assert json.loads(json.dumps(out)) == out


def test_summarise_stringifies_non_json_native_leaf():
    """A datetime leaf (even nested) mustn't crash — it gets stringified so the
    summary is serializable via plain `json.dumps` with no `default=`, which is
    what the asyncpg jsonb codec's own encoder calls."""
    checked_at = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    seen_at = datetime(2026, 1, 1)
    out = _summarise({"checked_at": checked_at, "nested": {"seen_at": seen_at}})
    assert out["checked_at"] == str(checked_at)
    assert out["nested"]["seen_at"] == str(seen_at)
    json.dumps(out)  # must not raise


def test_summarise_caps_oversized_summary():
    """A summary whose serialized size exceeds ~16KB gets its largest values
    replaced with a short marker instead of ballooning the jsonb column."""
    obj = {
        "small": "ok",
        "huge_field": ["x" * 100] * 300,  # ~30KB alone
        "another_huge": {"k": "y" * 20_000},
    }
    out = _summarise(obj)
    serialized = json.dumps(out)
    assert len(serialized) <= _MAX_SUMMARY_BYTES
    assert out["small"] == "ok"
    dropped = [v for v in out.values() if isinstance(v, str) and v.startswith("<dropped:")]
    assert dropped, f"expected at least one dropped marker, got {out}"


def test_json_safe_recurses_into_lists_and_dicts():
    value = {"a": [1, {"b": datetime(2026, 1, 1)}], "c": (1, 2)}
    out = _json_safe(value)
    assert out["a"][0] == 1
    assert out["a"][1]["b"] == str(datetime(2026, 1, 1))
    assert out["c"] == [1, 2]  # tuple -> list
    json.dumps(out)  # must not raise


def test_cap_summary_size_noop_when_under_budget():
    small = {"a": 1, "b": "hello"}
    assert _cap_summary_size(small) == small


@dataclass
class _NestedInput:
    agent_id: str


@workflow.defn(name="_NestedResultFlow")
class _NestedResultFlow:
    @workflow.run
    async def run(self, input: _NestedInput) -> dict:
        return {
            "status": "ok",
            "by_category": {"groceries": 3, "utilities": 1},
            "feeds": [{"name": "a", "count": 1}, {"name": "b", "count": 3}],
            "checked_at": datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC),
        }


async def test_interceptor_result_summary_round_trips_nested_dict_as_jsonb(
    temporal_env, db_pool, seeded_agent
):
    """End-to-end: a workflow returning a by_category-style nested dict must land
    in workflow_runs.result_summary as a real jsonb object/array — queryable via
    `->`/`->>`/`jsonb_typeof`, not a stringified repr (issue #112)."""
    recorder = RunRecorderActivities(db_pool=db_pool)
    tq = f"test-{uuid4().hex[:8]}"

    async with Worker(
        temporal_env.client,
        task_queue=tq,
        workflows=[_NestedResultFlow],
        activities=[recorder.record_workflow_run],
        interceptors=[WorkflowRunRecorderInterceptor()],
    ):
        await temporal_env.client.execute_workflow(
            _NestedResultFlow.run,
            _NestedInput(agent_id="sebas"),
            id=f"nested-{uuid4()}",
            task_queue=tq,
        )

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT result_summary, "
            "jsonb_typeof(result_summary->'by_category') AS by_category_type, "
            "result_summary->'by_category'->>'groceries' AS groceries, "
            "jsonb_typeof(result_summary->'feeds') AS feeds_type "
            "FROM workflow_runs WHERE workflow_type = '_NestedResultFlow' "
            "ORDER BY started_at DESC LIMIT 1"
        )
        await conn.execute("DELETE FROM workflow_runs WHERE workflow_type = '_NestedResultFlow'")

    assert row is not None
    assert row["by_category_type"] == "object"
    assert row["groceries"] == "3"
    assert row["feeds_type"] == "array"
    summary = row["result_summary"]
    assert isinstance(summary, dict)
    assert summary["by_category"] == {"groceries": 3, "utilities": 1}
    assert summary["checked_at"] == "2026-07-22 12:00:00+00:00"
